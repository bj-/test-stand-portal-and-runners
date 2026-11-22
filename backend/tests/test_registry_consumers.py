"""Один реестр определяет карточки, каталог, данные и безопасную выдачу очереди."""
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portal import auth, cli, jobs
from portal.auth import Principal, visitor
from portal.config import ProjectConfig
from portal.db import get_session
from portal.importer import sync_project
from portal.main import app
from portal.models import ApiToken, Base, Dataset, Job, Project, Run, StandBuild, StandInstance, StandTemplate, User
from portal.registry import load_registry
from portal.stands import lock_key

SUITE = "suites/smoke.yaml"
MOMENT = datetime(2026, 11, 18, 12)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    (tests / "suites").mkdir(parents=True)
    (tests / SUITE).write_text("description: smoke\ncollection: Example\n", encoding="utf-8")
    (tmp_path / "results").mkdir()
    file = tmp_path / "projects.yaml"
    file.write_text(yaml.safe_dump({"projects": {"YAML": {
        "title": "YAML project", "results": "results", "tests": "tests", "environments": ["dev", "alone"],
        "stands": {"legacy": {"title": "Legacy", "environments": ["dev"], "workdir": "/stand",
            "commands": {"restart": "trusted restart"}, "smoke": {"suite": SUITE, "env": "dev"},
            "test_data": {"records": {"format": "example.v1", "apply": "trusted apply", "inspect": "trusted inspect"}},
        }},
    }}}), encoding="utf-8")
    monkeypatch.setenv("PORTAL_PROJECTS", str(file))
    monkeypatch.setenv("PORTAL_ARTIFACTS", str(tmp_path / "artifacts"))
    return tmp_path


@pytest.fixture
def session(installation):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Project(code="DB", title="Database project", configuration={
            "tests": str(installation / "tests"), "environments": ["standalone"],
            "tests_url": "https://example.invalid/tests", "runner": "newman",
        }))
        session.add(User(login="owner", name="Owner", role="tester", password_hash="unused"))
        session.commit()
        session.add(StandTemplate(project="DB", key="demo", version="1", title="Demo", spec={}))
        session.commit()
        session.add(StandInstance(id="stable-managed-id", project="DB", name="Friendly managed name",
            template_key="demo", template_version="1", owner_login="owner", target_host="host",
            mode="permanent", profile="local", environments=["managed", "managed-two"],
            refs={"private": "must not appear"}, parameters={"token": "must not appear"}, status="created"))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[visitor] = lambda: Principal("tester", "tester")
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def worker(client, session):
    app.dependency_overrides.pop(visitor)
    client.headers["Authorization"] = "Bearer " + auth.create_token(session, "worker", "agent")
    return client


def request(project="DB", env="standalone"):
    return {"project": project, "suite": SUITE, "env": env}


def queued(session, project="DB", kind="run", target="standalone", **extra):
    job = Job(project=project, kind=kind, target=target, status=extra.pop("status", "queued"), title="queued",
        created_by="tester", created=MOMENT, spec={"stand": None, "lock": True, **extra.pop("spec", {})}, **extra)
    session.add(job)
    session.commit()
    return job


def test_projects_and_catalog_include_yaml_before_sync_without_get_writes(client, session):
    assert session.get(Project, "YAML") is None
    assert {p["code"] for p in client.get("/api/projects").json()} == {"YAML", "DB"}
    assert client.get("/api/projects/YAML/stands").json()[0]["name"] == "legacy"
    assert client.get("/api/projects/YAML/catalog").json()["environments"] == ["dev", "alone"]
    assert session.get(Project, "YAML") is None
    response = client.post("/api/jobs", json=request("YAML", "dev"))
    assert response.status_code == 201, response.text
    assert session.get(Project, "YAML").configuration is None


def test_managed_cards_catalog_and_data_share_stable_identity(client, session):
    cards = client.get("/api/projects/DB/stands")
    assert cards.status_code == 200
    card, = cards.json()
    assert (card["name"], card["title"], card["source"], card["state"]) == (
        "stable-managed-id", "Friendly managed name", "database", "created")
    assert card["environments"] == ["managed", "managed-two"]
    assert card["actions"] == [] and card["access"] is None and card["info"] is None
    assert "must not appear" not in cards.text
    catalog = client.get("/api/projects/DB/catalog").json()
    assert catalog["environments"] == ["standalone", "managed", "managed-two"]
    assert catalog["legacy_environments"] == ["standalone"]
    assert not catalog["run_stands"] and not card["can_run"]
    assert client.get("/api/projects/DB/stands/stable-managed-id/test-data").json()["targets"] == []
    project = load_registry(session).get("DB")
    assert lock_key(project, Job(project="DB", kind="run", target="managed-two")) == lock_key(
        project, Job(project="DB", kind="stand", target="stable-managed-id"))


@pytest.mark.parametrize("endpoint,body", [
    ("/api/jobs", request(env="managed")),
    ("/api/job-batches", {"project": "DB", "env": "managed", "suites": [{"suite": SUITE}]}),
    ("/api/projects/DB/stands/stable-managed-id/test-data/inspect", {"target": "records", "query": {}}),
])
def test_managed_execution_is_explicitly_unavailable(client, session, endpoint, body):
    response = client.post(endpoint, json=body)
    assert response.status_code == 409, response.text
    assert "ещё не подключено" in response.json()["detail"]
    assert session.scalars(select(Job)).all() == []


def test_database_project_can_run_against_its_standalone_environment(client, session):
    response = client.post("/api/jobs", json=request())
    assert response.status_code == 201, response.text
    claimed = worker(client, session).post("/api/agent/claim", json={"capabilities": ["newman", "stand-host"]})
    assert claimed.status_code == 200 and claimed.json()["id"] == response.json()["id"]
    assert claimed.json()["spec"]["stand"] is None


@pytest.mark.parametrize("state", ["ready", "limited"])
def test_managed_run_uses_deployed_binding_and_locks_stand(client, session, state):
    stand_id = "stable-managed-id"
    payload = {**request(env="baseUrl"), "stand_id": stand_id}
    assert client.post("/api/jobs", json=payload).status_code == 409
    instance = session.get(StandInstance, stand_id)
    sha = "b" * 40
    urls = {"baseUrl": "http://127.0.0.1:24000/", "secondary": "http://127.0.0.1:24001/api/"}
    session.add(StandBuild(id="build-example", project="DB", stand_id=stand_id,
        template_key="demo", template_version="1", status="done", finished=MOMENT,
        snapshot={"components": [{"id": "api", "sha": sha}]}, result={"output_bindings": urls}))
    instance.status, instance.deployed_build_id = state, "build-example"
    instance.mode, instance.expires = "ephemeral", instance.created + timedelta(days=1)
    session.commit()

    catalog_path = "/api/projects/DB/catalog"
    available, = client.get(catalog_path).json()["run_stands"]
    assert available == {"id": stand_id, "name": instance.name, "status": state,
        "bindings": [{"id": name, "title": name} for name in urls]}
    card, = client.get("/api/projects/DB/stands").json()
    assert card["can_run"] and card["mode"] == "ephemeral"
    assert datetime.fromisoformat(card["expires"]) == instance.expires.replace(tzinfo=UTC)
    assert client.post("/api/jobs", json={**payload, "env": "https://example.invalid/"}).status_code == 422
    assert client.post("/api/jobs", json={**payload, "project": "YAML"}).status_code == 404
    deletion = queued(session, kind="stand-delete", target=stand_id)
    assert not client.get(catalog_path).json()["run_stands"]
    assert client.post("/api/jobs", json=payload).status_code == 409
    deletion.status = "cancelled"
    session.commit()
    first = client.post("/api/jobs", json=payload)
    assert first.status_code == 201, first.text
    batch = client.post("/api/job-batches", json={**payload, "env": "secondary", "suites": [{"suite": SUITE}]})
    assert batch.status_code == 201, batch.text

    agent = worker(client, session)
    path = "/api/agent/claim"
    capabilities = ["newman", jobs.MANAGED_RUN_CAPABILITY]
    assert agent.post(path, json={"host_id": "host", "capabilities": capabilities}).status_code == 204
    token = session.scalar(select(ApiToken).where(ApiToken.name == "worker"))
    token.stand_host = "host"
    session.commit()
    assert agent.post(path, json={"host_id": "other", "capabilities": capabilities}).status_code == 204
    assert agent.post(path, json={"host_id": "host", "capabilities": ["newman"]}).status_code == 204
    first_response = agent.post(path, json={"host_id": "host", "capabilities": capabilities})
    assert first_response.status_code == 200, first_response.text
    first_job = first_response.json()
    assert first_job["id"] == first.json()["id"]
    assert first_job["spec"]["stand"] == stand_id
    assert first_job["spec"]["env"] == f"{stand_id}--baseUrl"
    assert first_job["spec"]["stand_target"] == {
        "binding": "baseUrl", "host_id": "host", "build_id": "build-example", "bindings": urls}
    assert first_job["spec"]["product"]["commits"] == {"api": sha}
    assert agent.post(path, json={"host_id": "host", "capabilities": capabilities}).status_code == 204
    assert agent.post(f"/api/agent/jobs/{first_job['id']}/finish", json={"status": "done"}).status_code == 200
    second = agent.post(path, json={"host_id": "host", "capabilities": capabilities}).json()
    assert second["id"] == batch.json()["jobs"][0]["id"]
    assert second["spec"]["stand_target"]["binding"] == "secondary"


def test_queue_run_guards_direct_call_before_creating_job(session):
    with pytest.raises(HTTPException) as error:
        jobs.queue_run(session, load_registry(session).get("DB"), SUITE, "managed", "main", "tester")
    assert error.value.status_code == 409
    assert session.scalars(select(Job)).all() == []


def test_conflict_is_scoped_for_api_and_claim(client, session):
    session.add(Project(code="YAML", title="conflict", configuration={}))
    session.commit()
    blocked = queued(session, project="YAML", target="dev")
    valid = queued(session)
    assert client.get("/api/projects/YAML/stands").status_code == 409
    assert client.get("/api/projects/YAML/catalog").status_code == 409
    assert client.get("/api/projects/YAML/datasets").status_code == 409
    assert client.get("/api/projects/DB/stands").status_code == 200
    assert {p["code"] for p in client.get("/api/projects").json()} == {"YAML", "DB"}
    response = worker(client, session).post("/api/agent/claim", json={"capabilities": ["newman", "stand-host"]})
    assert response.json()["id"] == valid.id
    assert blocked.status == "queued" and "configuration_conflict" in blocked.message


def test_claim_skips_managed_runs_stand_commands_and_cross_environment_snapshot(client, session):
    blocked = [queued(session, target="managed"),
        queued(session, kind="stand", target="stable-managed-id", spec={"command": "never execute"}),
        queued(session, target="standalone", spec={"stand": "stable-managed-id"})]
    valid = queued(session)
    response = worker(client, session).post("/api/agent/claim", json={"capabilities": ["newman", "stand-host"]})
    assert response.json()["id"] == valid.id
    assert all(job.status == "queued" and "ещё не подключено" in job.message for job in blocked)


def test_claim_child_cannot_execute_managed_data_operation(client, session):
    parent = queued(session, target="managed", status="running")
    parent.agent, parent.heartbeat = "worker", auth.now()
    session.commit()
    child = queued(session, kind="stand", target="stable-managed-id", parent_job_id=parent.id,
                   spec={"data_operation": True, "stand": "stable-managed-id"})
    response = worker(client, session).post(f"/api/agent/jobs/{parent.id}/data-claim",
        json={"capabilities": ["stand-host"]})
    assert response.status_code == 204
    assert child.status == "queued" and "ещё не подключено" in child.message


def test_missing_project_configuration_cannot_dispatch_old_stand_command(client, session):
    session.add(Project(code="INDEX", title="History only"))
    session.commit()
    blocked = queued(session, project="INDEX", kind="stand", target="old", spec={"command": "old command"})
    response = worker(client, session).post("/api/agent/claim", json={"capabilities": ["stand-host"]})
    assert response.status_code == 204
    assert blocked.status == "queued" and "конфигурация стенда" in blocked.message


def test_claim_rechecks_removed_environment_and_continues_other_projects(client, session):
    project = session.get(Project, "DB")
    project.configuration = {**project.configuration, "environments": ["standalone", "removed-environment"]}
    session.commit()
    created = client.post("/api/jobs", json=request(env="removed-environment"))
    assert created.status_code == 201
    removed = session.get(Job, created.json()["id"])
    project.configuration = {**project.configuration, "environments": ["standalone"]}
    session.add(Project(code="INDEX", title="History only"))
    session.commit()
    index = queued(session, project="INDEX", target="former-environment")
    valid = queued(session)
    response = worker(client, session).post("/api/agent/claim", json={"capabilities": ["newman"]})
    assert response.json()["id"] == valid.id
    assert all(job.status == "queued" and "больше не разрешено" in job.message for job in (removed, index))


def test_dataset_and_legacy_data_operations_use_the_same_registry(client, session):
    created = client.post("/api/projects/YAML/datasets", json={
        "key": "records", "title": "Records", "format": "example.v1", "data": {"records": []}})
    assert created.status_code == 201, created.text
    assert session.get(Project, "YAML") is not None
    assert session.get(Dataset, created.json()["id"]).project == "YAML"
    data = client.get("/api/projects/YAML/stands/legacy/test-data").json()
    assert data["targets"][0]["can_apply"]
    applied = client.post("/api/projects/YAML/stands/legacy/test-data/apply", json={
        "dataset_id": created.json()["id"], "version": 1, "target": "records"})
    assert applied.status_code == 201, applied.text
    response = worker(client, session).post("/api/agent/claim", json={"capabilities": ["stand-host"]})
    assert response.json()["id"] == applied.json()["id"]
    assert response.json()["spec"]["data_operations"][0]["adapter"]["command"] == "trusted apply"


def test_finish_conflicting_project_preserves_completed_job_without_smoke(client, session):
    session.add(Project(code="YAML", title="conflict", configuration={}))
    session.commit()
    job = queued(session, project="YAML", kind="stand", target="legacy", spec={"action": "restart"})
    job.status, job.agent, job.heartbeat = "running", "worker", auth.now()
    session.commit()
    response = worker(client, session).post(f"/api/agent/jobs/{job.id}/finish", json={"status": "done"})
    assert response.status_code == 200 and response.json()["status"] == "done"
    assert session.scalars(select(Job)).all() == [job]


def test_sync_without_results_preserves_history_and_reports_clear_skip(client, session):
    run = Run(id="DB-20261118-120000-smoke", project="DB", suite="smoke", path="history", source_hash="hash")
    session.add(run)
    session.commit()
    with pytest.raises(ValueError, match="не настроен"):
        sync_project(session, ProjectConfig("DB", "Changed title", None))
    assert session.get(Run, run.id) is run
    assert session.get(Project, "DB").title == "Database project"
    response = client.post("/api/sync?project=DB")
    assert response.status_code == 200 and "не настроен" in response.json()["DB"]["skipped"]
    assert session.get(Run, run.id) is run
    detail = client.get(f"/api/runs/{run.id}")
    assert detail.status_code == 200 and not detail.json()["has_report"] and not detail.json()["has_allure"]


def test_cli_sync_uses_database_registry_and_skips_missing_results(session, monkeypatch, capsys):
    class Context:
        def __enter__(self):
            return session

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli, "SessionLocal", Context)
    monkeypatch.setattr("sys.argv", ["portal", "sync", "--project", "DB"])
    assert cli.main() == 0
    assert "не настроен" in capsys.readouterr().out


def test_unknown_project_and_global_file_error_are_explicit(client, monkeypatch, installation):
    assert client.get("/api/projects/UNKNOWN/stands").status_code == 404
    monkeypatch.setenv("PORTAL_PROJECTS", str(installation / "secret-path-missing.yaml"))
    response = client.get("/api/projects")
    assert response.status_code == 409
    assert "secret-path" not in response.text
