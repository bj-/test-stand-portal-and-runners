"""Execution boundary, immutable inputs and retries against actual SQLite foreign keys."""
import copy
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from portal import auth
from portal.models import ApiToken, Job, StandBuild, StandInstance
from portal.stand_contract import LIVENESS_CAPABILITY, STAGES, StandError, template_hash, validate_snapshot, validate_template
from test_managed_stands import installation, session, client, register, create, body, template


@pytest.fixture(autouse=True)
def private_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_ARTIFACTS", str(tmp_path / "artifacts"))


def prepare(client, session, installation, spec=None):
    register(client, spec=spec)
    response = create(client)
    assert response.status_code == 201, response.text
    instance = response.json()
    job = session.get(Job, instance["job_id"])
    token = auth.create_token(session, "local-agent", "agent")
    row = session.get(ApiToken, auth.token_hash(token))
    row.stand_host = "local"
    session.commit()
    client.cookies.clear()
    client.headers["Authorization"] = "Bearer " + token
    return instance, job, row


def claim(client, host="local", *, liveness=False):
    capabilities = ["stand-template-v1"]
    if liveness:
        capabilities.append(LIVENESS_CAPABILITY)
    return client.post("/api/agent/claim", json={"host_id": host, "capabilities": capabilities})


def frozen(job, sha):
    return {"schema_version": 1, "template_sha256": job.spec["template_sha256"],
        "template": copy.deepcopy(job.spec["template"]), "profile": job.spec["request"]["profile"],
        "parameters": copy.deepcopy(job.spec["request"]["parameters"]), "secret_refs": [],
        "components": [{"id": key, "repository": value["repository"], "requested_ref": value["ref"], "sha": sha}
                       for key, value in job.spec["request"]["components"].items()]}


def snapshot(client, job, installation):
    value = frozen(job, installation[1])
    response = client.post(f"/api/agent/jobs/{job.id}/stand/snapshot", json={"snapshot": value})
    assert response.status_code == 200, response.text
    return response.json()["build_id"], value


def completion(job):
    return {"status": "done", "stages": [{"name": name, "status": "success"} for name in STAGES],
        "images": [{"id": "sha256:" + "a" * 64, "repo_digests": [], "repo_tags": ["demo:local"]}],
        "output_bindings": {key: f"{value['scheme']}://127.0.0.1:{job.spec['ports'][key]}{value['path']}"
                            for key, value in job.spec["template"]["output_bindings"].items()}}


def as_user(client, role="admin"):
    client.headers.pop("Authorization", None)
    client.cookies.set(auth.COOKIE, role)


def template_with_liveness():
    spec = template()
    spec["liveness"] = {"api": {"component": "api", "argv": ["docker", "compose", "run", "--no-deps", "api", "probe"]}}
    return spec


@pytest.mark.parametrize("check_status, final_status, instance_status, health_state, response_summary", [
    ("success", "done", "limited", "limited", "Отклик стенда подтверждён."),
    ("failed", "failed", "failed", "broken", "Отклик стенда не подтверждён."),
])
def test_smoke_fallback_marks_limited_only_after_confirmed_liveness(client, session, installation,
                                                            check_status, final_status, instance_status,
                                                            health_state, response_summary):
    instance, job, _ = prepare(client, session, installation, spec=template_with_liveness())
    assert claim(client).status_code == 204  # Older executors cannot interpret the new template.
    assert claim(client, liveness=True).status_code == 200
    build_id, value = snapshot(client, job, installation)
    url = f"/api/agent/jobs/{job.id}/stand"
    assert client.post(f"{url}/progress", json={"stage": "liveness", "status": "running"}).status_code == 409
    payload = completion(job)
    premature = {**payload, "stages": [*payload["stages"], {"name": "liveness", "status": "success"}]}
    assert client.post(f"{url}/finish", json=premature).status_code == 422  # Smoke must have failed first.
    payload["stages"][-1]["status"] = "failed"
    for stage in payload["stages"]:
        assert client.post(f"{url}/progress", json={"stage": stage["name"], "status": stage["status"]}).status_code == 200
    assert client.post(f"{url}/finish", json=payload).status_code == 422
    for status in ("running", check_status):
        assert client.post(f"{url}/progress", json={"stage": "liveness", "status": status}).status_code == 200
    payload["stages"].append({"name": "liveness", "status": check_status})
    if final_status == "failed":
        assert client.post(f"{url}/finish", json=payload).status_code == 422
    else:
        # Keep the failed smoke artifact when the smoke test managed to produce it.
        assert client.put(f"{url}/artifacts/allure-report.html", content=b"<html>failed smoke</html>").status_code == 204
        assert client.post(f"{url}/finish", json={**payload, "output_bindings": {}}).status_code == 422
    payload["status"] = final_status
    response = client.post(f"{url}/finish", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == final_status
    as_user(client)
    detail = client.get(f"/api/projects/ONE/stand-instances/{instance['id']}").json()
    assert detail["status"] == instance_status
    assert detail["deployed_build_id"] == (build_id if final_status == "done" else None)
    build = client.get(f"/api/stand-builds/{build_id}").json()
    assert build["stages"] == payload["stages"]
    card = client.get("/api/projects/ONE/stands").json()[0]
    assert card["health"]["state"] == health_state
    assert card["health"]["reasons"] == ["Smoke не пройден.", response_summary]
    if final_status == "done":
        assert {link["url"] for link in card["links"]} == {
            *payload["output_bindings"].values(), f"/api/stand-builds/{build_id}/artifacts/allure-report.html"}
        assert [(c["name"], c["ok"]) for c in card["health"]["checks"]] == [
            ("healthcheck", True), ("smoke", False), ("liveness", True)]
        retry = client.post(f"/api/projects/ONE/stand-instances/{instance['id']}/retry",
                            json={"build_id": build_id}, headers={"Idempotency-Key": "limited-retry"})
        assert retry.status_code == 201, retry.text
        assert session.get(Job, retry.json()["id"]).spec["snapshot"] == value
    else:
        assert card["links"] == []


@pytest.mark.parametrize("checks", [{}, {"wrong-service": {"component": "api", "argv": ["probe"]}},
                                    {"api": {"component": "wrong-component", "argv": ["probe"]}}])
def test_liveness_catalog_validates_service_and_command(checks):
    with pytest.raises(StandError):
        validate_template({**template_with_liveness(), "liveness": checks})


def test_claim_requires_registered_host_bound_token_and_updates_liveness(client, session, installation):
    instance, job, token = prepare(client, session, installation)
    assert claim(client, "other").status_code == 204
    assert token.last_seen is None
    token.stand_host = None
    session.commit()
    assert claim(client).status_code == 204
    token.stand_host = "local"
    session.commit()
    response = claim(client)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == job.id
    assert token.last_seen is not None
    as_user(client)
    response = client.post("/api/projects/ONE/stand-instances/preflight", json=body(name="Other", environments=["next"]))
    assert response.status_code == 200, response.text
    assert response.json()["executor_available"] is True
    token.last_seen = auth.now() - timedelta(seconds=61)
    session.commit()
    assert client.post("/api/projects/ONE/stand-instances/preflight", json=body(name="Other", environments=["next"])).json()["executor_available"] is False


def test_snapshot_checks_frozen_inputs_and_is_immutable(client, session, installation):
    _, job, _ = prepare(client, session, installation)
    assert claim(client).status_code == 200
    url = f"/api/agent/jobs/{job.id}/stand/snapshot"
    value = frozen(job, installation[1])
    for mutate in (lambda v: v["components"][0].update(sha="short"),
                   lambda v: v["components"][0].update(requested_ref="other"),
                   lambda v: v["components"][0].update(repository="other"),
                   lambda v: v["parameters"].update(label="changed"),
                   lambda v: v.update(profile="other")):
        altered = copy.deepcopy(value)
        mutate(altered)
        response = client.post(url, json={"snapshot": altered})
        assert response.status_code == 422, response.text
    build_id, _ = snapshot(client, job, installation)
    assert client.post(url, json={"snapshot": value}).status_code == 200
    value["components"][0]["sha"] = "b" * 40
    assert client.post(url, json={"snapshot": value}).status_code == 409
    assert session.get(StandBuild, build_id).snapshot["components"][0]["sha"] == installation[1]


@pytest.mark.parametrize("sha_length", [40, 64])
def test_snapshot_commit_must_match_explicit_full_sha(sha_length):
    spec = template()
    requested = "a" * sha_length
    value = {"schema_version": 1, "template": spec, "template_sha256": template_hash(spec),
        "profile": "laptop", "parameters": {"label": "demo"}, "secret_refs": [],
        "components": [{"id": "api", "repository": "source", "requested_ref": requested, "sha": "b" * sha_length}]}
    repositories = {"source": {"components": ["api"]}}
    with pytest.raises(StandError, match="explicitly requested SHA"):
        validate_snapshot(value, repositories)
    value["components"][0]["sha"] = requested.upper()
    assert validate_snapshot(value, repositories) == spec


def test_success_artifacts_public_projection_and_retry_frozen_sha(client, session, installation, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_ARTIFACTS", str(tmp_path / "artifacts"))
    instance, job, token = prepare(client, session, installation)
    assert claim(client).status_code == 200
    assert client.post(f"/api/agent/jobs/{job.id}/finish", json={"status": "done"}).status_code == 409
    assert client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=completion(job)).status_code == 422
    build_id, value = snapshot(client, job, installation)
    assert client.post(f"/api/agent/jobs/{job.id}/stand/progress", json={"stage": "deploy", "status": "running"}).status_code == 409
    for stage in STAGES:
        for status in ("running", "success", "success"):
            result = client.post(f"/api/agent/jobs/{job.id}/stand/progress", json={"stage": stage, "status": status})
            assert result.status_code == 200, result.text
    response = client.put(f"/api/agent/jobs/{job.id}/stand/artifacts/allure-report.html", content=b"<html>report</html>")
    assert response.status_code == 204, response.text
    altered = completion(job)
    altered["output_bindings"]["baseUrl"] = "http://evil.invalid/"
    assert client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=altered).status_code == 422
    response = client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=completion(job))
    assert response.status_code == 200, response.text
    assert session.get(StandInstance, instance["id"]).deployed_build_id == build_id
    assert client.get(f"/api/stand-builds/{build_id}").status_code == 403
    as_user(client)
    public = client.get(f"/api/stand-builds/{build_id}")
    assert public.status_code == 200, public.text
    for field in ("created", "started", "finished"):
        assert datetime.fromisoformat(public.json()[field]).utcoffset() == timedelta(0)
    assert "private-command" not in public.text and "private-compose" not in public.text
    artifact = client.get(public.json()["artifacts"][0]["url"])
    assert artifact.content == b"<html>report</html>"
    assert artifact.headers["content-disposition"].startswith("inline;")
    assert artifact.headers["content-type"].startswith("text/html")
    card = client.get("/api/projects/ONE/stands").json()[0]
    assert card["info"]["components"] == [{"name": "api", "commit": installation[1]}]
    assert card["health"]["state"] == "ok"
    assert any(link["title"] == "Allure развёрнутой сборки" for link in card["links"])
    retry_url = f"/api/projects/ONE/stand-instances/{instance['id']}/retry"
    response = client.post(retry_url, json={"build_id": build_id}, headers={"Idempotency-Key": "retry-1"})
    assert response.status_code == 201, response.text
    retry_id = response.json()["id"]
    assert retry_id != job.id
    assert session.get(Job, retry_id).spec["snapshot"] == value
    assert client.post(retry_url, json={"build_id": build_id}, headers={"Idempotency-Key": "retry-1"}).json()["id"] == retry_id
    assert client.post(retry_url, json={"build_id": "other"}, headers={"Idempotency-Key": "retry-1"}).status_code == 409
    assert client.post(retry_url, json={"build_id": build_id}, headers={"Idempotency-Key": "retry-2"}).status_code == 409
    assert client.get(f"/api/projects/ONE/stand-instances/{instance['id']}").json()["job_id"] == retry_id
    client.cookies.clear()
    assert client.get(f"/api/stand-builds/{build_id}/artifacts/allure-report.html").status_code == 401


def test_lost_agent_fails_instance_and_build_and_cannot_report(client, session, installation):
    instance, job, _ = prepare(client, session, installation)
    assert claim(client).status_code == 200
    build_id, _ = snapshot(client, job, installation)
    job.heartbeat = auth.now() - timedelta(minutes=6)
    session.commit()
    assert claim(client).status_code == 204
    assert job.status == "failed"
    assert session.get(StandBuild, build_id).status == "failed"
    assert session.get(StandInstance, instance["id"]).status == "failed"
    assert client.post(f"/api/agent/jobs/{job.id}/stand/finish", json={"status": "failed"}).status_code == 409


def test_cancel_and_preparation_failure_preserve_resources(client, session, installation):
    instance, job, token = prepare(client, session, installation)
    assert claim(client).status_code == 200
    as_user(client)
    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 200
    client.cookies.clear()
    # The original secret is held only by the HTTP client; use a second bound token/owner for an explicit failure.
    token_secret = auth.create_token(session, "second", "agent")
    agent = session.get(ApiToken, auth.token_hash(token_secret))
    agent.stand_host = "local"
    job.agent = "second"
    session.commit()
    client.headers["Authorization"] = "Bearer " + token_secret
    result = client.post(f"/api/agent/jobs/{job.id}/stand/finish", json={"status": "failed", "message": "preparation failed"})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "cancelled"
    assert session.scalar(select(StandBuild)) is None
    assert session.get(StandInstance, instance["id"]) is not None


def test_artifacts_caps_and_owner_guard(client, session, installation, monkeypatch):
    from portal import stand_execution
    _, job, _ = prepare(client, session, installation)
    assert claim(client).status_code == 200
    snapshot(client, job, installation)
    monkeypatch.setitem(stand_execution.ARTIFACTS, "results.json", 8)
    path = f"/api/agent/jobs/{job.id}/stand/artifacts/"
    assert client.put(path + "results.json", content=b"123456789").status_code == 413
    assert client.put(path + "unknown.txt", content=b"test").status_code == 422
    assert client.put(path + "results.json", content=b"12345678").status_code == 204
    other = auth.create_token(session, "other", "agent")
    session.get(ApiToken, auth.token_hash(other)).stand_host = "local"
    session.commit()
    client.headers["Authorization"] = "Bearer " + other
    assert client.put(path + "results.json", content=b"1").status_code == 409


@pytest.mark.parametrize("payload", [{"status": "failed", "message": "bad\x00message"},
    {"status": "done", "images": [{"id": "fake", "repo_tags": [], "repo_digests": []}]}])
def test_invalid_results_are_sanitized(client, session, installation, payload):
    _, job, _ = prepare(client, session, installation)
    claim(client)
    result = client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=payload)
    assert result.status_code == 422
    assert "input" not in result.text


def test_claim_never_crosses_host_and_preserves_stand_lock(client, session, installation):
    instance, job, token = prepare(client, session, installation)
    job.spec = {**job.spec, "target_host": "remote"}
    session.commit()
    assert claim(client).status_code == 204
    assert token.last_seen is not None
    job.spec = {**job.spec, "target_host": "local"}
    session.commit()
    assert claim(client).status_code == 200
    competing = Job(project=job.project, target=job.target, kind="stand-build", status="queued",
        spec=copy.deepcopy(job.spec), title="Competing", created=auth.now(), created_by="admin")
    session.add(competing)
    session.commit()
    assert claim(client).status_code == 204
    assert competing.status == "queued" and "освобождения" in competing.message
    build_id, _ = snapshot(client, job, installation)
    result = client.post(f"/api/agent/jobs/{job.id}/stand/finish", json={"status": "failed"})
    assert result.status_code == 200, result.text
    assert claim(client).json()["id"] == competing.id


def test_completed_stages_cannot_change_and_cancel_never_deploys(client, session, installation):
    instance, job, _ = prepare(client, session, installation)
    secret = client.headers["Authorization"]
    claim(client)
    build_id, _ = snapshot(client, job, installation)
    url = f"/api/agent/jobs/{job.id}/stand/progress"
    for state in ("running", "success"):
        assert client.post(url, json={"stage": "build", "status": state}).status_code == 200
    assert client.post(url, json={"stage": "build", "status": "failed"}).status_code == 409
    assert client.post(url, json={"stage": "prepare", "status": "running"}).status_code == 409
    as_user(client)
    client.post(f"/api/jobs/{job.id}/cancel")
    client.cookies.clear()
    client.headers["Authorization"] = secret
    response = client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=completion(job))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    row = session.get(StandInstance, instance["id"])
    assert row.status == "failed" and row.deployed_build_id is None
    assert session.get(StandBuild, build_id).status == "cancelled"


def test_retry_owner_and_role_guards_and_failure_retains_deployed_identity(client, session, installation):
    instance, job, _ = prepare(client, session, installation)
    secret = client.headers["Authorization"]
    claim(client)
    build_id, _ = snapshot(client, job, installation)
    client.put(f"/api/agent/jobs/{job.id}/stand/artifacts/allure-report.html", content=b"report")
    assert client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=completion(job)).status_code == 200
    url = f"/api/projects/ONE/stand-instances/{instance['id']}/retry"
    for role in ("developer", "viewer"):
        as_user(client, role)
        assert client.post(url, json={"build_id": build_id}, headers={"Idempotency-Key": role}).status_code == 403
    as_user(client)
    retry = client.post(url, json={"build_id": build_id}, headers={"Idempotency-Key": "retry"}).json()
    client.cookies.clear()
    client.headers["Authorization"] = secret
    claimed = claim(client).json()
    assert claimed["id"] == retry["id"]
    assert "retry_request_key" not in claimed["spec"] and "retry_request_hash" not in claimed["spec"]
    retry_job = session.get(Job, retry["id"])
    retry_build_id, _ = snapshot(client, retry_job, installation)
    assert client.post(f"/api/agent/jobs/{retry_job.id}/stand/finish", json={"status": "failed"}).status_code == 200
    assert session.get(StandInstance, instance["id"]).deployed_build_id == build_id
    as_user(client)
    card = client.get("/api/projects/ONE/stands").json()[0]
    assert card["info"]["build_id"] == build_id
    assert retry_build_id != build_id


def test_provision_is_idempotent_does_not_rotate_or_modify_users(client, session, tmp_path, monkeypatch, capsys):
    from portal import cli
    from portal.models import User
    from sqlalchemy import func
    token_file = tmp_path / "private-token"
    token_file.write_text("x" * 48 + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SessionLocal", lambda: session)
    arguments = ["portal.cli", "stand-agent", "provision", "--name", "local", "--host", "local", "--token-file", str(token_file)]
    monkeypatch.setattr("sys.argv", arguments)
    before = [(u.login, u.password_hash) for u in session.scalars(select(User))]
    assert cli.main() == 0
    assert cli.main() == 0
    token = session.scalar(select(ApiToken).where(ApiToken.name == "local"))
    assert token.stand_host == "local" and token.role == "agent"
    assert session.scalar(select(func.count()).select_from(ApiToken)) == 1
    token_file.write_text("y" * 48, encoding="utf-8")
    assert cli.main() == 1
    assert token.token_hash == auth.token_hash("x" * 48)
    assert [(u.login, u.password_hash) for u in session.scalars(select(User))] == before
    output = capsys.readouterr()
    assert "x" * 48 not in output.out + output.err and "y" * 48 not in output.out + output.err
