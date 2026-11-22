"""Пакеты проверяются только на искусственных репозиториях и локальной SQLite."""
import subprocess
from uuid import UUID

import pytest
from sqlalchemy import func, select

from portal import auth, jobs
from portal.models import Job
from test_jobs import (DANGEROUS, SUITE, agent, claim, installation, run_request, session, user, web)

SECOND = "suites/Orders/second.yaml"


@pytest.fixture
def two_suites(installation):
    root = installation / "tests"
    (root / SECOND).write_text("collection: Orders\ngate: suites/gate.yaml\n", encoding="utf-8")
    collection = root / "postman/collections/Orders/api"
    collection.mkdir(parents=True)
    (collection / "P01.request.yaml").write_text("method: GET\n", encoding="utf-8")
    return root


def batch_request(**extra):
    return {"project": "ACME", "env": "dev", "suites": [{"suite": SUITE}, {"suite": SECOND}], **extra}


def test_batch_preserves_suite_order_and_duplicate_test_runs(web, session, two_suites):
    client = user(web, session, "ann", "tester")
    response = client.post("/api/job-batches", json=batch_request(
        tags=["sg-a"], suites=[{"suite": SECOND, "test_ids": ["api/P01"]},
                              {"suite": SUITE, "test_ids": ["api/P01"]}]))
    assert response.status_code == 201, response.text
    batch = response.json()
    assert str(UUID(batch["id"])) == batch["id"]
    assert (batch["total"], batch["finished"], batch["status"]) == (2, 0, "queued")
    assert batch["created"].endswith("Z")
    assert batch["counts"] == {"queued": 2, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    members = session.scalars(select(Job).order_by(Job.id)).all()
    assert [job.spec["suite"] for job in members] == [SECOND, SUITE]
    assert all(job.batch_id == batch["id"] and job.spec["test_ids"] == ["api/P01"] and
               job.spec["tags"] == ["sg-a"] for job in members)
    assert client.get(f"/api/job-batches/{batch['id']}").json() == batch
    assert all(job["batch_id"] == batch["id"] for job in client.get("/api/jobs").json()["items"])
    single = client.post("/api/jobs", json=run_request()).json()
    assert single["batch_id"] is None


@pytest.mark.parametrize("invalid", [
    {"suite": "suites/missing.yaml"}, {"suite": DANGEROUS}, {"suite": SECOND, "test_ids": []},
    {"suite": SECOND, "test_ids": ["missing"]}, {"suite": SECOND, "tests": ["bad\nfilter"]},
    {"suite": SECOND, "test_ids": ["api/P01"], "tests": ["P01"]},
])
def test_invalid_last_member_never_queues_first(web, session, two_suites, monkeypatch, invalid):
    client = user(web, session, "ann", "tester")
    def cannot_queue(*args, **kwargs):
        pytest.fail("валидация пакета должна завершиться до queue_run")
    monkeypatch.setattr(jobs, "queue_run", cannot_queue)
    response = client.post("/api/job-batches", json=batch_request(suites=[{"suite": SUITE}, invalid]))
    assert response.status_code == 422, response.text
    assert session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("suites", [[], [{"suite": SUITE}] * 2,
                                    [{"suite": f"suites/{i}.yaml"} for i in range(21)]])
def test_batch_size_and_unique_suites(web, session, suites):
    assert user(web, session, "ann", "tester").post(
        "/api/job-batches", json=batch_request(suites=suites)).status_code == 422
    assert session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("role,status", [("admin", 201), ("tester", 201), ("developer", 201), ("viewer", 403)])
def test_batch_create_roles(web, session, two_suites, role, status):
    client = user(web, session, "ann", role)
    assert client.post("/api/job-batches", json=batch_request()).status_code == status


def test_batch_read_permissions_and_missing(web, session, two_suites):
    batch = user(web, session, "ann", "tester").post("/api/job-batches", json=batch_request()).json()
    url = f"/api/job-batches/{batch['id']}"
    assert web().get(url).status_code == 200  # тот же гостевой просмотр, что у jobs
    assert user(web, session, "vic", "viewer").get(url).status_code == 200
    for role in ("llm", "agent"):
        client = web()
        client.headers["Authorization"] = f"Bearer {auth.create_token(session, role, role)}"
        assert client.get(url).status_code == 403
        assert client.post("/api/job-batches", json=batch_request()).status_code == 403
    assert web().post("/api/job-batches", json=batch_request()).status_code == 401
    assert web().get("/api/job-batches/no-such-batch").status_code == 404


def test_progress_logs_runs_and_sequential_claims(web, session, two_suites):
    client = user(web, session, "ann", "tester")
    batch = client.post("/api/job-batches", json=batch_request()).json()
    url = f"/api/job-batches/{batch['id']}"
    first, second = [job["id"] for job in batch["jobs"]]
    worker = agent(web, session)
    assert claim(worker)["id"] == first
    assert claim(worker) is None
    progress = client.get(url).json()
    assert (progress["status"], progress["finished"]) == ("running", 0)
    assert progress["counts"]["running"] == 1
    for job_id, outcome in ((first, "done"), (second, "failed")):
        if job_id == second:
            assert client.get(url).json()["status"] == "running"  # между двумя выдачами
            assert claim(worker)["id"] == second
        assert worker.post(f"/api/agent/jobs/{job_id}/log", json={"text": f"log {job_id}"}).status_code == 200
        assert worker.post(f"/api/agent/jobs/{job_id}/finish", json={
            "status": outcome, "run_id": f"ACME-20260101-100000-{job_id}"}).status_code == 200
        assert client.get(f"/api/jobs/{job_id}").json()["log"] == f"log {job_id}"
    final = client.get(url).json()
    assert (final["status"], final["finished"], final["counts"]["done"], final["counts"]["failed"]) == ("mixed", 2, 1, 1)
    assert len({job["run_id"] for job in final["jobs"]}) == 2


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
def test_uniform_terminal_batch_status(web, session, two_suites, outcome):
    client = user(web, session, "ann", "tester")
    batch = client.post("/api/job-batches", json=batch_request()).json()
    worker = agent(web, session)
    for item in batch["jobs"]:
        if outcome == "cancelled":
            assert client.post(f"/api/jobs/{item['id']}/cancel").status_code == 200
        else:
            assert claim(worker)["id"] == item["id"]
            worker.post(f"/api/agent/jobs/{item['id']}/finish", json={"status": outcome})
    final = client.get(f"/api/job-batches/{batch['id']}").json()
    assert (final["status"], final["finished"], final["counts"][outcome]) == (outcome, 2, 2)


def git(root, *args):
    subprocess.run(["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
                   check=True, capture_output=True, timeout=20)


@pytest.mark.parametrize("exact", [False, True])
def test_full_and_exact_batch_use_ref_safety(web, session, two_suites, exact):
    root = two_suites
    if exact:
        (root / "postman/collections/Orders/api/P01.request.yaml").write_text(
            'method: GET\nscripts:\n  - type: beforeRequest\n    code: needFlag("allowDestructive")\n', encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "safe")
    git(root, "checkout", "-b", "feature")
    # В main набор безопасный; в feature — разрушающий. Рабочее дерево снова main.
    (root / SECOND).write_text("only: destructive\ncollection: Orders\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "dangerous")
    git(root, "checkout", "main")
    selection = {"suite": SECOND, **({"test_ids": ["api/P01"]} if exact else {})}
    developer = user(web, session, "dev", "developer")
    tester = user(web, session, "tester", "tester")
    assert developer.post("/api/job-batches", json=batch_request(ref="feature", suites=[{"suite": SUITE}, selection])).status_code == 403
    assert tester.post("/api/job-batches", json=batch_request(ref="feature", suites=[{"suite": SUITE}, selection])).status_code == 422
    assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert tester.post("/api/job-batches", json=batch_request(ref="feature", destructive=True, suites=[selection])).status_code == 201
    assert tester.post("/api/jobs", json=run_request(suite=SECOND, ref="feature")).status_code == 422
    assert tester.post("/api/jobs", json=run_request(suite=SECOND, ref="missing")).status_code == 422


@pytest.mark.parametrize("extra", [{"ref": "main\n"}, {"ref": "missing"}, {"tags": ["ok\n"]},
                                  {"groups": ["ok\n"]}, {"env": "PROD"}])
def test_shared_options_rejected_atomically(web, session, two_suites, extra):
    client = user(web, session, "ann", "tester")
    assert client.post("/api/job-batches", json=batch_request(**extra)).status_code == 422
    assert session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("tests", [[], ["legacy"]])
def test_full_and_legacy_do_not_require_static_discovery(web, session, installation, tests):
    root = installation / "mobile"
    (root / "suites/android.yaml").write_text("paths: [tests/test_app.py::dynamic]\n", encoding="utf-8")
    # Ни корректность Python, ни поддержка nodeid в static discovery не нужны для полного запуска.
    (root / "broken.py").write_text("this is not valid python !", encoding="utf-8")
    client = user(web, session, "ann", "tester")
    options = {"project": "MOB", "env": "dev"}
    selection = {"suite": "suites/android.yaml", "tests": tests}
    assert client.post("/api/jobs", json={**options, **selection}).status_code == 201
    assert client.post("/api/job-batches", json={**options, "suites": [selection]}).status_code == 201
    assert client.post("/api/jobs", json={**options, **selection, "tests": [], "test_ids": ["unknown"]}).status_code == 422
