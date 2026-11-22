"""Протокол проверяется на временном каталоге и локальной БД, без обновления машины."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from portal import auth, jobs, update_control
from portal.models import Job
from test_jobs import agent, claim, installation, run_request, session, user, web


@pytest.fixture
def control(tmp_path, monkeypatch):
    directory = tmp_path / "control"
    monkeypatch.setenv("PORTAL_UPDATE_DIR", str(directory))
    return directory


def token_client(web, session, role):
    client = web()
    client.headers["Authorization"] = f"Bearer {auth.create_token(session, 'token-' + role, role)}"
    return client


@pytest.mark.parametrize("role", ["viewer", "tester", "developer", "agent", "llm"])
def test_only_admin_and_updater_may_control_updates(web, session, control, role):
    client = token_client(web, session, role)
    assert client.get("/api/admin/updates").status_code == 403
    assert client.post("/api/admin/updates/check", json={}).status_code == 403
    assert client.post("/api/admin/updates/apply", json={}).status_code == 403
    assert client.get(f"/api/admin/updates/{uuid4()}").status_code == 403


def test_updater_token_is_scoped_even_for_auth_mutations(web, session, control):
    client = token_client(web, session, "updater")
    assert client.get("/api/admin/updates").status_code == 200
    assert client.post("/api/admin/updates/check").status_code == 202
    for method, path, payload in [
        ("GET", "/api/projects", None), ("GET", "/api/users", None), ("GET", "/api/profile", None),
        ("GET", "/api/projects/ACME/stands", None), ("GET", "/api/projects/ACME/catalog", None),
        ("GET", "/api/jobs", None), ("POST", "/api/jobs", run_request()),
        ("POST", "/api/users", {"login": "unexpected", "role": "admin"}),
        ("PATCH", "/api/users/root", {"active": False}), ("POST", "/api/auth/logout", {}),
        ("POST", "/api/agent/claim", {"capabilities": ["newman"]}),
        ("POST", "/api/auth/password", {"current": "password", "new": "long-password"}),
        ("PUT", "/api/admin/updates/policy", {"paused": True}),
    ]:
        assert client.request(method, path, json=payload).status_code == 403, path


def test_anonymous_requests_require_login(web, control):
    client = web()
    assert client.get("/api/admin/updates").status_code == 401
    assert client.get(f"/api/admin/updates/{uuid4()}").status_code == 401
    for action in ("check", "apply"):
        assert client.post(f"/api/admin/updates/{action}", json={}).status_code == 401


def test_disabled_and_missing_operations(web, session, monkeypatch):
    monkeypatch.delenv("PORTAL_UPDATE_DIR", raising=False)
    client = user(web, session, "root", "admin")
    assert client.get("/api/admin/updates").json()["enabled"] is False
    assert client.post("/api/admin/updates/check").status_code == 503
    assert client.post("/api/admin/updates/apply").status_code == 503
    assert client.put("/api/admin/updates/policy", json={"paused": True}).status_code == 503


def test_admin_can_pause_and_resume_installation_only_with_supported_worker(web, session, control):
    admin = user(web, session, "root", "admin")
    policy = "/api/admin/updates/policy"
    assert admin.get("/api/admin/updates").json()["pause_supported"] is False
    assert admin.put(policy, json={"paused": True}).status_code == 503
    update_control.atomic_json(control / "status.json", {"enabled": True, "pause_supported": True})
    for invalid in ({"paused": "true"}, {"paused": 1}, {"paused": True, "ref": "other"}):
        assert admin.put(policy, json=invalid).status_code == 422

    assert admin.put(policy, json={"paused": True}).json() == {"paused": True}
    worker_client = token_client(web, session, "updater")
    status = worker_client.get("/api/admin/updates").json()
    assert status["enabled"] is True and status["pause_supported"] is True and status["paused"] is True
    assert worker_client.post("/api/admin/updates/apply").status_code == 409
    assert worker_client.post("/api/admin/updates/check").status_code == 202

    assert admin.put(policy, json={"paused": False}).json() == {"paused": False}
    saved = json.loads((control / "policy.json").read_text(encoding="utf-8"))
    assert saved["paused"] is False
    assert admin.put(policy, json={"paused": False}).status_code == 200
    response = worker_client.post("/api/admin/updates/apply")
    assert response.status_code == 202
    assert "policy_generation" not in response.json()
    stored = json.loads((control / "requests" / f"{response.json()['id']}.json").read_text(encoding="utf-8"))
    assert stored["policy_generation"] == saved["generation"]


@pytest.mark.parametrize("action", ["check", "apply"])
def test_requests_are_atomic_persistent_and_accept_no_configuration(web, session, control, action):
    client = user(web, session, "root", "admin")
    for payload in ({"ref": "other"}, {"repo": "/tmp/elsewhere"}, {"command": "unsafe"}, [], "apply"):
        assert client.post(f"/api/admin/updates/{action}", json=payload).status_code == 422
    assert not (control / "requests").exists()
    response = client.post(f"/api/admin/updates/{action}", json={})
    assert response.status_code == 202
    operation = response.json()
    stored = json.loads((control / "requests" / f"{operation['id']}.json").read_text(encoding="utf-8"))
    assert stored == {key: operation[key] for key in ("id", "action", "requested_by", "created_at")}
    assert stored["action"] == action and stored["requested_by"] == "root"
    # A new API client reads durable request state; API never writes operations/status.
    restarted = token_client(web, session, "updater")
    assert restarted.get(f"/api/admin/updates/{operation['id']}").json()["phase"] == "queued"
    assert not (control / "operations").exists() and not (control / "status.json").exists()
    assert not list(control.rglob("*.tmp"))
    assert client.get(f"/api/admin/updates/{uuid4()}").status_code == 404
    assert client.get("/api/admin/updates/not-a-uuid").status_code == 404


def test_host_status_and_operation_hide_diagnostics(web, session, control):
    client = user(web, session, "root", "admin")
    identifier = client.post("/api/admin/updates/apply").json()["id"]
    data = {"id": identifier, "phase": "failed", "message": "secret host log\n" * 500,
            "log": "secret", "error": "secret", "current_revision": "a" * 40,
            "available_revision": "https://secret:password@host/repo", "last_operation_id": identifier}
    update_control.atomic_json(control / "operations" / f"{identifier}.json", data)
    update_control.atomic_json(control / "status.json", data)
    for path in ("/api/admin/updates", f"/api/admin/updates/{identifier}"):
        response = client.get(path)
        assert response.status_code == 200
        assert "secret" not in response.text and "log" not in response.json()
        assert response.json()["current_revision"] == "a" * 40
        assert response.json()["phase"] == "failed"
        assert len(response.json()["message"]) < 200
    (control / "status.json").write_text("broken", encoding="utf-8")
    assert client.get("/api/admin/updates").status_code == 503
    (control / "status.json").write_text("x" * 40_000, encoding="utf-8")
    assert client.get("/api/admin/updates").status_code == 503


def test_failure_metadata_is_fixed_and_cleared_for_new_operation(web, session, control):
    client = user(web, session, "root", "admin")
    path = control / "status.json"
    update_control.atomic_json(path, {"phase": "failed", "failure_phase": "backing_up", "error_code": "command_timeout"})
    status = client.get("/api/admin/updates").json()
    assert status["failure_phase"] == "backing_up" and status["error_code"] == "command_timeout"
    for data in (
        {"phase": "checking", "failure_phase": "backing_up", "error_code": "update_failed"},
        {"phase": "failed", "failure_phase": "raw secret log", "error_code": "secret password"},
        {"phase": "failed", "failure_phase": ["preparing"], "error_code": {"invalid": "shape"}},
    ):
        update_control.atomic_json(path, data)
        status = client.get("/api/admin/updates").json()
        assert "failure_phase" not in status and "error_code" not in status


def test_drain_blocks_top_level_but_queue_and_running_survive(web, session, control, monkeypatch):
    tester = user(web, session, "ann", "tester")
    first = tester.post("/api/jobs", json=run_request()).json()["id"]
    worker = agent(web, session)
    assert claim(worker)["id"] == first
    session.get(Job, first).heartbeat = auth.now() - timedelta(days=1)
    session.commit()
    assert update_control.maintenance("drain", session) == {"running": 1, "maintenance": True}
    second = tester.post("/api/jobs", json=run_request()).json()["id"]
    lock_calls = []
    original_lock = jobs._claim_lock
    monkeypatch.setattr(jobs, "_claim_lock", lambda db: (lock_calls.append(True), original_lock(db)))
    response = worker.post("/api/agent/claim", json={"capabilities": ["newman"]})
    assert response.status_code == 204 and lock_calls
    assert session.get(Job, first).status == "running"  # stale heartbeat is not forced to finish
    assert session.get(Job, second).status == "queued"
    assert update_control.maintenance("idle", session) == {"running": 1, "maintenance": True}
    # Count remains running until the agent explicitly finishes.
    assert worker.post(f"/api/agent/jobs/{first}/finish", json={"status": "done"}).status_code == 200
    assert update_control.maintenance("idle", session) == {"running": 0, "maintenance": True}
    assert update_control.maintenance("resume", session) == {"running": 0, "maintenance": False}
    assert claim(worker)["id"] == second


def test_drain_allows_active_parent_child_but_rejects_inactive_parent(web, session, control):
    tester = user(web, session, "ann", "tester")
    parent_id = tester.post("/api/jobs", json=run_request()).json()["id"]
    worker = agent(web, session)
    assert claim(worker)["id"] == parent_id
    child = Job(parent_job_id=parent_id, project="ACME", kind="stand", status="queued", target="dev",
                title="Данные", spec={"data_operation": True}, created_by="ann", created=auth.now())
    session.add(child)
    session.commit()
    update_control.maintenance("drain", session)
    wrong = agent(web, session, "other")
    assert wrong.post(f"/api/agent/jobs/{parent_id}/data-claim", json={"capabilities": ["stand-host"]}).status_code == 204
    response = worker.post(f"/api/agent/jobs/{parent_id}/data-claim", json={"capabilities": ["stand-host"]})
    assert response.status_code == 200 and response.json()["id"] == child.id
    assert update_control.maintenance("idle", session)["running"] == 2
    assert worker.post(f"/api/agent/jobs/{child.id}/finish", json={"status": "done"}).status_code == 200
    assert worker.post(f"/api/agent/jobs/{parent_id}/finish", json={"status": "done"}).status_code == 200
    assert worker.post(f"/api/agent/jobs/{parent_id}/data-claim", json={"capabilities": ["stand-host"]}).status_code == 204
    assert update_control.maintenance("idle", session)["running"] == 0


def test_malformed_maintenance_flag_never_expires(control):
    control.mkdir()
    (control / "maintenance.json").write_text("invalid but persistent", encoding="utf-8")
    assert update_control.maintenance_active()


def test_claim_observes_maintenance_only_after_shared_lock(web, session, control, monkeypatch):
    tester = user(web, session, "ann", "tester")
    identifier = tester.post("/api/jobs", json=run_request()).json()["id"]
    worker = agent(web, session)
    # Simulate drain winning the race immediately before claim acquires the lock.
    monkeypatch.setattr(jobs, "_claim_lock", lambda db: update_control.atomic_json(control / "maintenance.json", {}))
    assert worker.post("/api/agent/claim", json={"capabilities": ["newman"]}).status_code == 204
    assert session.get(Job, identifier).status == "queued"
