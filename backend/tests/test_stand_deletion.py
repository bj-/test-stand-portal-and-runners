"""Deletion is host-bound cleanup; registry and ports change only after confirmed success."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from portal import auth
from portal.models import ApiToken, Job, StandBuild, StandInstance, StandPortReservation
from test_managed_stands import installation, session, client, register, create, body
from test_stand_execution import as_user, claim, completion, prepare, snapshot


@pytest.fixture(autouse=True)
def private_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_ARTIFACTS", str(tmp_path / "artifacts"))


def delete_url(instance):
    return f"/api/projects/ONE/stand-instances/{instance['id']}"


def finished_instance(client, session, installation):
    instance, job, token = prepare(client, session, installation)
    assert claim(client).status_code == 200
    build_id, _ = snapshot(client, job, installation)
    assert client.put(f"/api/agent/jobs/{job.id}/stand/artifacts/allure-report.html", content=b"report").status_code == 204
    assert client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=completion(job)).status_code == 200
    secret = client.headers["Authorization"]
    as_user(client)
    return instance, build_id, token, secret


def claim_delete(client, secret, host="local", capabilities=None):
    client.cookies.clear()
    client.headers["Authorization"] = secret
    return client.post("/api/agent/claim", json={"host_id": host,
        "capabilities": ["stand-template-v1-delete"] if capabilities is None else capabilities})


@pytest.mark.parametrize("role,owner,expected", [("tester", "tester", 202), ("developer", "developer", 202),
    ("viewer", "viewer", 202), ("tester", "admin", 403), ("developer", "admin", 403),
    ("viewer", "admin", 403), ("admin", "tester", 202)])
def test_delete_authorizes_admin_or_actual_creator(client, session, role, owner, expected):
    register(client)
    instance = create(client).json()
    session.get(StandInstance, instance["id"]).owner_login = owner
    session.get(Job, instance["job_id"]).status = "cancelled"
    session.commit()
    as_user(client, role)
    detail = client.get(delete_url(instance)).json()
    assert detail["can_delete"] == (expected == 202)
    result = client.delete(delete_url(instance))
    assert result.status_code == expected, result.text
    if expected == 202:
        assert session.get(Job, result.json()["deletion_job_id"]).spec["workspace_required"] is False


def test_deletion_waits_for_active_jobs_and_keeps_legacy_stands(client, session):
    register(client)
    instance = create(client).json()
    assert client.delete(delete_url(instance)).status_code == 409
    original = session.get(Job, instance["job_id"])
    original.status = "done"
    pending = Job(project="ONE", kind="run", status="queued", target="feature", title="tests",
                  created_by="admin", created=auth.now(), spec={})
    session.add(pending)
    session.commit()
    assert client.delete(delete_url(instance)).status_code == 409
    pending.status = "failed"
    session.commit()
    assert client.delete(delete_url(instance)).status_code == 202
    assert client.delete("/api/projects/YAML/stand-instances/legacy").status_code == 404
    assert client.get("/api/projects/YAML/stands").json()[0]["name"] == "legacy"


def test_success_hides_stand_releases_ports_keeps_allure_and_allows_name_reuse(client, session, installation, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_ARTIFACTS", str(tmp_path / "artifacts"))
    instance, build_id, token, secret = finished_instance(client, session, installation)
    result = client.delete(delete_url(instance))
    assert result.status_code == 202, result.text
    deletion = result.json()
    deletion_id = deletion["deletion_job_id"]
    assert deletion["status"] == "deleting" and deletion["deletion_status"] == "queued"
    assert deletion["can_delete"] is False
    assert client.delete(delete_url(instance)).json()["deletion_job_id"] == deletion_id
    assert len(client.get("/api/projects/ONE/stands").json()) == 1
    assert session.scalar(select(StandPortReservation)) is not None
    retry = delete_url(instance) + "/retry"
    assert client.post(retry, json={"build_id": build_id}, headers={"Idempotency-Key": "retry"}).status_code == 409
    assert claim_delete(client, secret, capabilities=["stand-template-v1"]).status_code == 204
    assert claim_delete(client, secret, host="other").status_code == 204
    response = claim_delete(client, secret)
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "stand-delete"
    assert response.json()["spec"] == {"schema_version": 1, "project": "ONE", "stand": instance["id"],
                                     "target_host": "local", "lock": True, "parent_job_id": None,
                                     "workspace_required": True}
    assert client.post(f"/api/agent/jobs/{deletion_id}/log", json={"text": ""}).json() == {"cancel": False}
    assert client.post(f"/api/agent/jobs/{deletion_id}/finish", json={"status": "done"}).status_code == 409
    path = f"/api/agent/jobs/{deletion_id}/stand/delete-finish"
    assert client.post(path, json={"status": "done"}).status_code == 200
    assert client.post(path, json={"status": "done"}).status_code == 409
    assert session.get(StandInstance, instance["id"]).deleted_at is not None
    assert session.scalar(select(StandPortReservation)) is None
    assert session.get(StandBuild, build_id) is not None
    as_user(client)
    assert client.get("/api/projects/ONE/stands").json() == []
    assert client.get(f"/api/stand-builds/{build_id}/artifacts/allure-report.html").content == b"report"
    assert client.get(delete_url(instance) + "/builds").json()[0]["id"] == build_id
    assert client.get(delete_url(instance)).json()["status"] == "deleted"
    assert client.delete(delete_url(instance)).json()["deletion_job_id"] == deletion_id
    assert client.post(retry, json={"build_id": build_id}, headers={"Idempotency-Key": "retry"}).status_code == 409
    replacement = create(client, key="new-request")
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["id"] != instance["id"]
    assert replacement.json()["ports"] == instance["ports"]


@pytest.mark.parametrize("outcome", ["failed", "lost", "cancelled"])
def test_failed_lost_or_cancelled_cleanup_preserves_stand_and_ports_and_can_be_retried(client, session, installation, outcome):
    instance, build_id, token, secret = finished_instance(client, session, installation)
    first = client.delete(delete_url(instance)).json()["deletion_job_id"]
    if outcome == "cancelled":
        assert client.post(f"/api/jobs/{first}/cancel").status_code == 200
    else:
        assert claim_delete(client, secret).status_code == 200
        if outcome == "lost":
            session.get(Job, first).heartbeat = auth.now() - timedelta(minutes=6)
            session.commit()
            assert claim_delete(client, secret).status_code == 204
            assert client.post(f"/api/agent/jobs/{first}/stand/delete-finish", json={"status": "done"}).status_code == 409
        else:
            assert client.post(f"/api/agent/jobs/{first}/stand/delete-finish", json={"status": "failed", "message": "cleanup failed"}).status_code == 200
        as_user(client)
    assert session.get(StandInstance, instance["id"]).deleted_at is None
    assert session.scalar(select(StandPortReservation)) is not None
    assert len(client.get("/api/projects/ONE/stands").json()) == 1
    assert client.get(delete_url(instance)).json()["can_delete"] is True
    second = client.delete(delete_url(instance))
    assert second.status_code == 202, second.text
    assert second.json()["deletion_job_id"] != first


def test_delete_completion_requires_claim_and_host_binding(client, session, installation):
    instance, _, token, secret = finished_instance(client, session, installation)
    deletion_id = client.delete(delete_url(instance)).json()["deletion_job_id"]
    endpoint = f"/api/agent/jobs/{deletion_id}/stand/delete-finish"
    assert claim_delete(client, secret).status_code == 200
    token.stand_host = "other"
    session.commit()
    assert client.post(endpoint, json={"status": "done"}).status_code == 403
    token.stand_host = "local"
    session.commit()
    assert client.post(endpoint, json={"status": "cancelled"}).status_code == 422
    assert client.post(endpoint, json={"status": "failed", "message": "bad\x00message"}).status_code == 422
    as_user(client)
    assert client.post(f"/api/jobs/{deletion_id}/cancel").status_code == 200
    assert claim_delete(client, secret).status_code == 204
    assert client.post(endpoint, json={"status": "done"}).status_code == 200
    assert session.get(StandInstance, instance["id"]).deleted_at is not None
