import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portal import auth, jobs
from portal import help as help_module
from portal.db import get_session
from portal.main import app
from portal.models import Base, Job, Project, Run

PASSWORD = "длинный-пароль-1"
SUITE = "suites/Orders/smoke.yaml"
DANGEROUS = "suites/Orders/destructive.yaml"


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(Project(code="ACME", title="Acme"))
        db.commit()
        yield db


@pytest.fixture(autouse=True)
def installation(tmp_path: Path, monkeypatch):
    suites = tmp_path / "tests" / "suites" / "Orders"
    suites.mkdir(parents=True)
    (suites / "smoke.yaml").write_text("description: смоук\ncollection: Orders\n", encoding="utf-8")
    (suites / "destructive.yaml").write_text("only: destructive\ncollection: Orders\n", encoding="utf-8")
    (tmp_path / "results").mkdir()
    mobile = tmp_path / "mobile" / "suites"
    mobile.mkdir(parents=True)
    (mobile / "android.yaml").write_text("description: UI на телефоне\npaths: [tests/app]\n", encoding="utf-8")
    file = tmp_path / "projects.yaml"
    file.write_text(yaml.safe_dump({"projects": {"ACME": {
        "title": "Acme", "results": "results", "tests": "tests", "environments": ["dev", "main"],
        "tests_url": "https://git/tests.git", "results_url": "https://git/results.git",
        "stands": {"dev": {
            "title": "Стенд dev", "environments": ["dev"], "workdir": "/opt/stands",
            "links": [{"title": "API", "url": "http://stand:3000"}],
            "params": [{"name": "APP_REF", "title": "Ветка приложения", "default": "dev"}],
            "commands": {"deploy": "./build.sh dev && ./stand.sh dev deploy", "restart": "./stand.sh dev up",
                         "reset": "./stand.sh dev reset", "status": "./stand.sh dev ps", "logs": "./stand.sh dev logs",
                         "seed": "./seed.sh dev"},
            "access": [{"title": "База", "value": "postgresql://app:${DB_PASSWORD}@stand:5432/${DB_NAME}", "secret": True}],
            "secrets_files": [str(tmp_path / "stand.env")],
            "info_file": str(tmp_path / "info.json"),
            "smoke": {"suite": SUITE, "env": "dev"},
        }},
    }, "MOB": {
        "title": "Мобилка", "results": "results", "tests": "mobile", "environments": ["dev"],
        "tests_url": "https://git/mobile.git", "results_url": "https://git/results.git",
        "runner": "pytest", "capability": "appium-android",
    }}, "help": {
        "sections": [{"title": "Полезное", "items": [
            {"title": "Сертификат", "url": "http://stand/ca.crt", "note": "один раз"},
            {"title": "Как установить", "page": "https"},
            {"title": "Нет такой страницы", "page": "missing"},
        ]}],
        "pages": {"https": {"title": "HTTPS", "file": str(tmp_path / "https.md")}, "../etc": {"file": "/etc/passwd"}},
    }}), encoding="utf-8")
    (tmp_path / "https.md").write_text("# HTTPS\n\nПоставьте сертификат.", encoding="utf-8")
    (tmp_path / "stand.env").write_text("# секреты стенда\nDB_PASSWORD='s3cret'\nDB_NAME=app\n", encoding="utf-8")
    monkeypatch.setenv("PORTAL_PROJECTS", str(file))
    monkeypatch.setenv("PORTAL_ARTIFACTS", str(tmp_path / "artifacts"))
    return tmp_path


@pytest.fixture
def web(session):
    app.dependency_overrides[get_session] = lambda: session
    yield lambda: TestClient(app)
    app.dependency_overrides.clear()


def user(web, session, login: str, role: str) -> TestClient:
    auth.create_user(session, login, role, PASSWORD)
    client = web()
    assert client.post("/api/auth/login", json={"login": login, "password": PASSWORD}).status_code == 200
    return client


def agent(web, session, name: str = "agent-1") -> TestClient:
    client = web()
    client.headers["Authorization"] = f"Bearer {auth.create_token(session, name, 'agent')}"
    return client


def run_request(**extra) -> dict:
    return {"project": "ACME", "suite": SUITE, "env": "dev", **extra}


def claim(client: TestClient) -> dict | None:
    response = client.post("/api/agent/claim", json={"capabilities": ["newman"]})
    return response.json() if response.status_code == 200 else None


def test_job_times_are_marked_as_utc(web, session):
    created = user(web, session, "ann", "tester").post("/api/jobs", json=run_request()).json()["created"]
    assert created.endswith("Z")


def test_catalog_lists_suites_and_allowed_environments(web, session):
    catalog = user(web, session, "ann", "viewer").get("/api/projects/ACME/catalog").json()
    assert catalog["environments"] == ["dev", "main"]
    assert {s["path"]: s["destructive"] for s in catalog["suites"]} == {SUITE: False, DANGEROUS: True}


@pytest.mark.parametrize(("role", "status"), [("admin", 201), ("tester", 201), ("developer", 201), ("viewer", 403)])
def test_who_can_start_a_run(web, session, role, status):
    assert user(web, session, "ann", role).post("/api/jobs", json=run_request()).status_code == status


def test_llm_token_cannot_start_or_read_jobs(web, session):
    client = web()
    client.headers["Authorization"] = f"Bearer {auth.create_token(session, 'llm-1', 'llm')}"
    assert client.post("/api/jobs", json=run_request()).status_code == 403
    assert client.get("/api/jobs").status_code == 403


@pytest.mark.parametrize("body", [
    {"suite": "suites/../../etc/passwd.yaml"},
    {"suite": "suites/Orders/none.yaml"},
    {"env": "PROD"},
    {"ref": "--upload-pack=x"},
    {"ref": "main..dev"},
    {"tags": ["a b"]},
    {"groups": ["--destructive"]},
])
def test_invalid_run_is_rejected(web, session, body):
    assert user(web, session, "ann", "tester").post("/api/jobs", json=run_request(**body)).status_code == 422


def test_destructive_rules(web, session):
    tester = user(web, session, "ann", "tester")
    developer = user(web, session, "bob", "developer")
    assert developer.post("/api/jobs", json=run_request(destructive=True)).status_code == 403
    assert developer.post("/api/jobs", json=run_request(suite=DANGEROUS, destructive=True)).status_code == 403
    assert tester.post("/api/jobs", json=run_request(suite=DANGEROUS)).status_code == 422
    created = tester.post("/api/jobs", json=run_request(suite=DANGEROUS, destructive=True))
    assert created.status_code == 201
    assert session.get(Job, created.json()["id"]).spec["destructive"] is True


def test_job_lifecycle(web, session):
    tester = user(web, session, "ann", "tester")
    worker = agent(web, session)
    job_id = tester.post("/api/jobs", json=run_request(ref="main", tags=["sg-a"])).json()["id"]

    claimed = claim(worker)
    assert claimed["id"] == job_id
    assert claimed["spec"]["ref"] == "main" and claimed["spec"]["requested_by"] == "ann"
    assert claimed["spec"]["tests_url"] == "https://git/tests.git"
    assert claim(worker) is None

    assert worker.post(f"/api/agent/jobs/{job_id}/log", json={"text": "строка 1\n"}).json() == {"cancel": False}
    worker.post(f"/api/agent/jobs/{job_id}/log", json={"text": "строка 2\n"})
    assert tester.get(f"/api/jobs/{job_id}").json()["log"] == "строка 1\nстрока 2\n"

    finished = worker.post(f"/api/agent/jobs/{job_id}/finish", json={"status": "done", "run_id": "ACME-20260101-100000-x"}).json()
    assert finished["status"] == "done" and finished["run_id"] == "ACME-20260101-100000-x"
    assert worker.post(f"/api/agent/jobs/{job_id}/log", json={"text": "поздно"}).status_code == 409


def test_stand_runs_one_job_at_a_time_but_others_continue(web, session):
    tester = user(web, session, "ann", "tester")
    worker = agent(web, session)
    first = tester.post("/api/jobs", json=run_request()).json()["id"]
    second = tester.post("/api/jobs", json=run_request()).json()["id"]
    other = tester.post("/api/jobs", json=run_request(env="main")).json()["id"]

    assert claim(worker)["id"] == first
    assert claim(worker)["id"] == other
    assert claim(worker) is None
    worker.post(f"/api/agent/jobs/{first}/finish", json={"status": "done"})
    assert claim(worker)["id"] == second


def test_agent_without_capability_gets_nothing(web, session):
    user(web, session, "ann", "tester").post("/api/jobs", json=run_request())
    assert agent(web, session).post("/api/agent/claim", json={"capabilities": ["appium"]}).status_code == 204


def test_run_goes_to_agent_with_capability_of_project_runner(web, session):
    tester = user(web, session, "ann", "tester")
    newman_job = tester.post("/api/jobs", json=run_request()).json()["id"]
    mobile_job = tester.post("/api/jobs", json={"project": "MOB", "suite": "suites/android.yaml", "env": "dev"}).json()["id"]

    phone = agent(web, session, "phone").post("/api/agent/claim", json={"capabilities": ["appium-android"]}).json()
    assert phone["id"] == mobile_job
    assert phone["spec"]["runner"] == "pytest"
    assert phone["spec"]["tests_url"] == "https://git/mobile.git"
    assert claim(agent(web, session, "api"))["id"] == newman_job


def test_foreign_agent_cannot_touch_job(web, session):
    tester = user(web, session, "ann", "tester")
    job_id = tester.post("/api/jobs", json=run_request()).json()["id"]
    claim(agent(web, session, "agent-1"))
    stranger = agent(web, session, "agent-2")
    assert stranger.post(f"/api/agent/jobs/{job_id}/finish", json={"status": "done"}).status_code == 409


def test_cancel_queued_and_running(web, session):
    tester = user(web, session, "ann", "tester")
    worker = agent(web, session)
    running = tester.post("/api/jobs", json=run_request()).json()["id"]
    queued = tester.post("/api/jobs", json=run_request()).json()["id"]
    claim(worker)

    assert tester.post(f"/api/jobs/{queued}/cancel").json()["status"] == "cancelled"
    assert tester.post(f"/api/jobs/{running}/cancel").json()["cancel_requested"] is True
    assert worker.post(f"/api/agent/jobs/{running}/log", json={"text": ""}).json() == {"cancel": True}


def test_developer_cancels_only_own_jobs(web, session):
    tester = user(web, session, "ann", "tester")
    developer = user(web, session, "bob", "developer")
    job_id = tester.post("/api/jobs", json=run_request()).json()["id"]
    assert developer.post(f"/api/jobs/{job_id}/cancel").status_code == 403


def test_lost_agent_releases_stand(web, session):
    tester = user(web, session, "ann", "tester")
    worker = agent(web, session)
    first = tester.post("/api/jobs", json=run_request()).json()["id"]
    second = tester.post("/api/jobs", json=run_request()).json()["id"]
    claim(worker)
    session.get(Job, first).heartbeat = auth.now() - jobs.LOST_AFTER - timedelta(seconds=1)
    session.commit()

    assert claim(worker)["id"] == second
    lost = tester.get(f"/api/jobs/{first}").json()
    assert lost["status"] == "failed" and "агент" in lost["message"]


def test_artifact_upload_and_download(web, session, installation):
    tester = user(web, session, "ann", "tester")
    worker = agent(web, session)
    run_id = "ACME-20260101-100000-x"
    session.add(Run(id=run_id, project="ACME", suite="x", path="runs/none", source_hash="0", started=auth.now()))
    session.commit()
    job_id = tester.post("/api/jobs", json=run_request()).json()["id"]
    claim(worker)

    url = f"/api/agent/jobs/{job_id}/artifact"
    assert worker.put(f"{url}/{run_id}/allure-report.html", content=b"<html>").status_code == 204
    assert worker.put(f"{url}/{run_id}/evil.sh", content=b"x").status_code == 422
    assert worker.put(f"{url}/bad..id/allure-report.html", content=b"x").status_code == 422
    assert (installation / "artifacts" / run_id / "allure-report.html").read_bytes() == b"<html>"
    assert tester.get(f"/api/runs/{run_id}").json()["has_allure"] is True
    assert tester.get(f"/api/runs/{run_id}/allure").content == b"<html>"


# --- Стенды ---------------------------------------------------------------------------------------------------------

def stand_claim(client: TestClient) -> dict | None:
    response = client.post("/api/agent/claim", json={"capabilities": ["newman", "stand-host"]})
    return response.json() if response.status_code == 200 else None


def test_stands_list_from_manifest(web, session):
    stands = user(web, session, "vic", "viewer").get("/api/projects/ACME/stands").json()
    assert [s["name"] for s in stands] == ["dev"]
    dev = stands[0]
    assert dev["links"] == [{"title": "API", "url": "http://stand:3000"}]
    assert [a["name"] for a in dev["actions"]] == ["deploy", "restart", "seed", "reset", "status", "logs"]
    assert not any(a["allowed"] for a in dev["actions"])  # просмотр без действий
    assert "command" not in str(dev)  # команды установки наружу не отдаются


@pytest.mark.parametrize("role, action, status", [
    ("viewer", "status", 403), ("developer", "status", 201), ("developer", "deploy", 403),
    ("tester", "deploy", 201), ("admin", "restart", 201), ("tester", "seed", 201), ("developer", "seed", 403),
])
def test_who_can_act_on_stand(web, session, role, action, status):
    client = user(web, session, "u1", role)
    assert client.post(f"/api/projects/ACME/stands/dev/{action}", json={}).status_code == status


def test_reset_needs_confirmation(web, session):
    client = user(web, session, "ann", "tester")
    assert client.post("/api/projects/ACME/stands/dev/reset", json={}).status_code == 422
    assert client.post("/api/projects/ACME/stands/dev/reset", json={"confirm": True}).status_code == 201


@pytest.mark.parametrize("params", [{"APP_REF": "dev; rm -rf /"}, {"APP_REF": "../x"}, {"OTHER": "x"}, {"APP_REF": "-x"}])
def test_deploy_params_are_checked(web, session, params):
    client = user(web, session, "ann", "tester")
    assert client.post("/api/projects/ACME/stands/dev/deploy", json={"params": params}).status_code == 422


def test_deploy_job_spec_carries_command_and_params(web, session):
    client = user(web, session, "ann", "tester")
    client.post("/api/projects/ACME/stands/dev/deploy", json={"params": {"APP_REF": "BA-15"}})
    job = stand_claim(agent(web, session))
    assert job["kind"] == "stand"
    assert job["spec"]["command"] == "./build.sh dev && ./stand.sh dev deploy"
    assert job["spec"]["workdir"] == "/opt/stands"
    assert job["spec"]["env"] == {"APP_REF": "BA-15"}
    # default подставляется, если значение не передано
    client.post("/api/projects/ACME/stands/dev/deploy", json={})
    assert session.scalars(select(Job).order_by(Job.id.desc())).first().spec["env"] == {"APP_REF": "dev"}


def test_stand_actions_need_stand_host(web, session):
    user(web, session, "ann", "tester").post("/api/projects/ACME/stands/dev/restart", json={})
    assert claim(agent(web, session)) is None  # только newman


def test_run_and_deploy_share_stand_lock(web, session):
    tester = user(web, session, "ann", "tester")
    tester.post("/api/jobs", json=run_request())  # прогон на окружении dev = стенд dev
    tester.post("/api/projects/ACME/stands/dev/deploy", json={})
    tester.post("/api/projects/ACME/stands/dev/status", json={})
    worker = agent(web, session)
    assert stand_claim(worker)["kind"] == "run"
    # пересборка ждёт прогон, состояние - нет
    assert stand_claim(worker)["spec"]["action"] == "status"
    assert stand_claim(worker) is None
    stands = tester.get("/api/projects/ACME/stands").json()
    assert stands[0]["busy"] is True


def test_deploy_blocks_later_run_on_same_stand(web, session):
    tester = user(web, session, "ann", "tester")
    tester.post("/api/projects/ACME/stands/dev/deploy", json={})
    tester.post("/api/jobs", json=run_request())
    tester.post("/api/jobs", json=run_request(env="main"))
    worker = agent(web, session)
    assert stand_claim(worker)["spec"]["action"] == "deploy"
    assert stand_claim(worker)["spec"]["env"] == "main"  # другое окружение не ждёт
    assert stand_claim(worker) is None


def finish(client: TestClient, job: dict, status: str = "done") -> None:
    assert client.post(f"/api/agent/jobs/{job['id']}/finish", json={"status": status}).status_code == 200


def dev_stand(client: TestClient) -> dict:
    return client.get("/api/projects/ACME/stands").json()[0]


@pytest.mark.parametrize("role, visible", [("tester", True), ("developer", True), ("admin", True), ("viewer", False)])
def test_access_only_for_allowed_roles(web, session, role, visible):
    access = dev_stand(user(web, session, "u1", role))["access"]
    if visible:
        assert access == [{"title": "База", "value": "postgresql://app:s3cret@stand:5432/app", "secret": True}]
    else:
        assert access is None


def test_health_unknown_without_info_and_runs(web, session):
    health = dev_stand(user(web, session, "ann", "tester"))["health"]
    assert health["state"] == "unknown" and health["reasons"] == []


def write_info(installation, checks_ok: bool = True, deployed: str = "2026-11-05T10:00:00+03:00") -> None:
    (installation / "info.json").write_text(json.dumps({
        "built": "2026-11-05T09:55:00+03:00", "deployed": deployed, "checked": "2026-11-05T10:01:00+03:00",
        "components": [{"name": "app", "commit": "abc1234", "version": "1.2.3", "api_version": "1.2.3"}],
        "health": [{"name": "API отвечает", "ok": checks_ok, "detail": "" if checks_ok else "connection refused"}],
    }), encoding="utf-8")


def add_run(session, run_id: str, started: datetime, **extra) -> None:
    session.add(Run(id=run_id, project="ACME", suite=extra.pop("suite", "smoke"), kind=extra.pop("kind", "smoke"),
                    started=started, stand_name="dev", path=run_id, source_hash="x", tests=4, **extra))
    session.commit()


def test_failed_check_marks_stand_broken(web, session, installation):
    write_info(installation, checks_ok=False)
    stand = dev_stand(user(web, session, "ann", "tester"))
    assert stand["info"]["components"][0]["version"] == "1.2.3"
    assert stand["health"]["state"] == "broken"
    assert "API отвечает: connection refused" in stand["health"]["reasons"]


def test_smoke_after_deploy_decides_health(web, session, installation):
    write_info(installation)
    tester = user(web, session, "ann", "tester")
    add_run(session, "ACME-20261105-090000-smoke", datetime(2026, 11, 5, 9, 0), failed=4)  # до развёртывания - не в счёт
    assert dev_stand(tester)["health"]["state"] == "unknown"
    add_run(session, "ACME-20261105-100500-smoke", datetime(2026, 11, 5, 10, 5), passed=4)
    assert dev_stand(tester)["health"]["state"] == "ok"
    add_run(session, "ACME-20261105-110000-full", datetime(2026, 11, 5, 11, 0), suite="full", kind="full",
            passed=0, gate_failed=True)
    stand = dev_stand(tester)
    assert stand["health"]["state"] == "broken"
    assert stand["runs"][0]["id"] == "ACME-20261105-110000-full"


def test_known_problem_marks_stand_broken(web, session, installation):
    file = installation / "projects.yaml"
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    data["projects"]["ACME"]["stands"]["dev"]["problem"] = "Мобильное API падает: **нет таблицы**"
    file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    write_info(installation)
    stand = dev_stand(user(web, session, "ann", "tester"))
    assert stand["health"]["state"] == "broken"
    assert stand["problem"].startswith("Мобильное API")


def test_smoke_is_queued_after_deploy_but_not_after_status(web, session):
    tester = user(web, session, "ann", "tester")
    tester.post("/api/projects/ACME/stands/dev/status", json={})
    tester.post("/api/projects/ACME/stands/dev/deploy", json={})
    worker = agent(web, session)
    finish(worker, stand_claim(worker))  # состояние
    assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "run")) == 0
    deploy = stand_claim(worker)
    finish(worker, deploy)
    smoke = session.scalars(select(Job).where(Job.kind == "run")).one()
    assert smoke.target == "dev" and smoke.spec["suite"] == SUITE and smoke.created_by == "ann"
    assert f"после задачи {deploy['id']}" in smoke.title


def test_no_smoke_after_failed_deploy(web, session):
    user(web, session, "ann", "tester").post("/api/projects/ACME/stands/dev/deploy", json={})
    worker = agent(web, session)
    finish(worker, stand_claim(worker), status="failed")
    assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "run")) == 0


def test_help_sections_and_pages(web, session):
    client = user(web, session, "vic", "viewer")
    sections = client.get("/api/help").json()
    assert sections == [{"title": "Полезное", "items": [
        {"title": "Сертификат", "url": "http://stand/ca.crt", "note": "один раз"},
        {"title": "Как установить", "url": "/help/https", "note": ""},
    ]}]
    page = client.get("/api/help/pages/https").json()
    assert page["title"] == "HTTPS" and "Поставьте сертификат" in page["markdown"]
    # Выход из каталога: путь нормализуется мимо API, файл не отдаётся; имя страницы вне шаблона отбрасывается.
    escaped = client.get("/api/help/pages/..%2Fetc")
    assert "markdown" not in escaped.text and "root:" not in escaped.text
    assert "../etc" not in help_module._pages()
    assert client.get("/api/help/pages/missing").status_code == 404


def test_help_needs_login(web, session):
    assert web().get("/api/help").status_code == 200  # ТЗ: просмотр справки доступен гостю.


def test_run_gets_stand_product_at_start(web, session, installation):
    tester = user(web, session, "ann", "tester")
    tester.post("/api/jobs", json=run_request())
    write_info(installation)  # стенд пересобран после постановки в очередь - важно, что на нём при старте
    job = claim(agent(web, session))
    assert job["spec"]["product"] == {"build": "Стенд dev: app 1.2.3", "commits": {"app": "abc1234"}}


def test_run_without_stand_info_has_no_product(web, session):
    user(web, session, "ann", "tester").post("/api/jobs", json=run_request(env="main"))
    assert "product" not in claim(agent(web, session))["spec"]


def test_run_with_selected_tests(web, session):
    tester = user(web, session, "ann", "tester")
    created = tester.post("/api/jobs", json=run_request(tests=["P01", "onStart - P02", "  "])).json()
    assert "тесты: P01, onStart - P02" in created["title"]
    assert claim(agent(web, session))["spec"]["tests"] == ["P01", "onStart - P02"]


@pytest.mark.parametrize("tests", [["bad\nline"], ["x" * 121], [f"T{i}" for i in range(31)]])
def test_selected_tests_are_checked(web, session, tests):
    assert user(web, session, "ann", "tester").post("/api/jobs", json=run_request(tests=tests)).status_code == 422


def test_catalog_tests_from_suite_collection(web, session, installation):
    folder = installation / "tests" / "postman" / "collections" / "Orders" / "api"
    folder.mkdir(parents=True)
    (folder / "orders - P01 200 список.request.yaml").write_text("x", encoding="utf-8")
    (folder / "orders - N01 401.request.yaml").write_text("x", encoding="utf-8")
    client = user(web, session, "dev1", "developer")
    assert client.get("/api/projects/ACME/catalog/tests", params={"suite": SUITE}).json() == [
        "orders - N01 401", "orders - P01 200 список"]
    assert client.get("/api/projects/ACME/catalog/tests", params={"suite": "suites/../x.yaml"}).status_code == 404


def test_exact_catalog_and_queue(web, session, installation):
    folder = installation / "tests" / "postman" / "collections" / "Orders" / "api"
    folder.mkdir(parents=True)
    for name in ("P01", "P010"):
        (folder / f"{name}.request.yaml").write_text("method: GET\n", encoding="utf-8")
    client = user(web, session, "ann", "tester")
    catalog = client.get("/api/projects/ACME/catalog/test-ids", params={"suite": SUITE, "ref": "main"}).json()
    assert catalog["runner"] == "newman" and catalog["ref"] == "main"
    assert [item["id"] for item in catalog["items"]] == ["api/P01", "api/P010"]
    response = client.post("/api/jobs", json=run_request(test_ids=["api/P01"]))
    assert response.status_code == 201
    assert claim(agent(web, session))["spec"]["test_ids"] == ["api/P01"]
    for extra in ({"test_ids": []}, {"test_ids": ["P01"]}, {"test_ids": ["api/P01"], "tests": ["P01"]},
                  {"test_ids": ["api/P01", "api/P01"]}, {"test_ids": ["../api/P01"]}):
        assert client.post("/api/jobs", json=run_request(**extra)).status_code == 422
    assert client.get("/api/projects/ACME/catalog/test-ids", params={"suite": SUITE, "ref": "missing"}).status_code == 422
    assert client.get("/api/projects/ACME/catalog/test-ids", params={"suite": "../outside", "ref": "main"}).status_code == 422


def test_exact_catalog_permissions(web, session):
    client = web()
    client.headers["Authorization"] = f"Bearer {auth.create_token(session, 'llm-exact', 'llm')}"
    assert client.get("/api/projects/ACME/catalog/test-ids", params={"suite": SUITE}).status_code == 403
    assert client.post("/api/jobs", json=run_request(test_ids=["x"])).status_code == 403


def test_exact_uses_suite_safety_from_requested_ref(web, session, monkeypatch):
    monkeypatch.setattr(jobs, "_exact_catalog", lambda project, suite, ref: {
        "destructive": True, "items": [{"id": "exact"}]})
    developer = user(web, session, "dev", "developer")
    tester = user(web, session, "tester", "tester")
    assert developer.post("/api/jobs", json=run_request(ref="feature", test_ids=["exact"])).status_code == 403
    assert tester.post("/api/jobs", json=run_request(ref="feature", test_ids=["exact"])).status_code == 422
    # Набор может существовать только в выбранной ветке.
    assert tester.post("/api/jobs", json=run_request(suite="suites/branch-only.yaml", ref="feature", test_ids=["exact"], destructive=True)).status_code == 201


def test_pytest_static_catalog_does_not_execute_code(web, session, installation):
    folder = installation / "mobile" / "tests" / "app"
    folder.mkdir(parents=True)
    (folder / "test_app.py").write_text(
        'raise RuntimeError("must not import")\n'
        'def test_one(): pass\nclass TestApp:\n    def test_two(self): pass\n', encoding="utf-8")
    client = user(web, session, "ann", "viewer")
    result = client.get("/api/projects/MOB/catalog/test-ids", params={"suite": "suites/android.yaml"})
    assert result.status_code == 200
    assert [i["id"] for i in result.json()["items"]] == ["tests/app/test_app.py::TestApp::test_two", "tests/app/test_app.py::test_one"]


def test_llm_sees_stands_without_access_but_not_jobs(web, session):
    client = web()
    client.headers["Authorization"] = f"Bearer {auth.create_token(session, 'assistant', 'llm')}"
    stands = client.get("/api/projects/ACME/stands").json()
    assert stands[0]["access"] is None and not any(a["allowed"] for a in stands[0]["actions"])
    assert client.get("/api/jobs").status_code == 403
    assert client.post("/api/projects/ACME/stands/dev/status", json={}).status_code == 403
