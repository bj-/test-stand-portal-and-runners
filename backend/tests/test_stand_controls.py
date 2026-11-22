"""Managed actions use the same queue, stand lock and frozen build history."""
import copy

import pytest
from sqlalchemy import select

from portal import auth
from portal.models import ApiToken, Job, StandBuild, StandInstance
from test_managed_stands import installation, session, client
from test_stand_execution import prepare, claim, snapshot, completion, as_user


def deployed(client, session, installation):
    instance, job, token = prepare(client, session, installation)
    secret = client.headers["Authorization"]
    assert claim(client).status_code == 200
    build_id, frozen = snapshot(client, job, installation)
    assert client.put(f"/api/agent/jobs/{job.id}/stand/artifacts/allure-report.html", content=b"report").status_code == 204
    assert client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=completion(job)).status_code == 200
    as_user(client)
    return instance, job, build_id, frozen, token, secret


def action_url(instance, action):
    return f"/api/projects/ONE/stands/{instance['id']}/{action}"


def control_claim(client, secret, host="local", capabilities=None):
    client.cookies.clear()
    client.headers["Authorization"] = secret
    return client.post("/api/agent/claim", json={"host_id": host,
        "capabilities": capabilities if capabilities is not None else ["stand-template-v1-control"]})


@pytest.mark.parametrize("action,locking", [("restart", True), ("status", False), ("logs", False)])
def test_actions_dispatch_controls_and_log_results(client, session, installation, action, locking):
    instance, original, build_id, _, token, secret = deployed(client, session, installation)
    response = client.post(action_url(instance, action), json={})
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    assert "журнале" in response.json()["message"]
    job = session.get(Job, job_id)
    assert job.kind == "stand-control"
    assert job.spec == {"schema_version": 1, "stand": instance["id"], "project": "ONE",
                        "target_host": "local", "action": action, "lock": locking}
    assert control_claim(client, secret, capabilities=["stand-template-v1"]).status_code == 204
    assert control_claim(client, secret, host="remote").status_code == 204
    claimed = control_claim(client, secret)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json() == {"id": job_id, "kind": "stand-control", "spec": {**job.spec, "parent_job_id": None}}
    assert token.last_seen is not None
    log = "api: running (healthy)" if action == "status" else "service output"
    assert client.post(f"/api/agent/jobs/{job_id}/log", json={"text": log}).status_code == 200
    response = client.post(f"/api/agent/jobs/{job_id}/finish", json={"status": "done"})
    assert response.status_code == 200, response.text
    row = session.get(StandInstance, instance["id"])
    assert row.status == "ready" and row.deployed_build_id == build_id
    assert len(session.scalars(select(StandBuild)).all()) == 1
    as_user(client)
    assert log in client.get(f"/api/jobs/{job_id}").json()["log"]
    stand = client.get("/api/projects/ONE/stands").json()[0]
    assert stand["jobs"][0]["id"] == job_id
    assert {a["name"] for a in stand["actions"]} == {"deploy", "restart", "status", "logs"}


def test_deploy_shares_retry_snapshot_and_creates_new_history(client, session, installation):
    instance, original, first_build_id, frozen, _, secret = deployed(client, session, installation)
    original.spec = {**original.spec, "test_data": {"title": "Saved data plan", "entries": []}}
    session.commit()
    expected = copy.deepcopy(original.spec)
    response = client.post(action_url(instance, "deploy"), json={})
    assert response.status_code == 201, response.text
    job = session.get(Job, response.json()["id"])
    assert job.kind == "stand-build" and job.spec == {**expected, "snapshot": frozen}
    assert job.target == original.target
    assert job.spec["ports"] == original.spec["ports"]
    assert client.post(action_url(instance, "deploy"), json={}).status_code == 409
    client.cookies.clear()
    client.headers["Authorization"] = secret
    assert claim(client).json()["id"] == job.id
    second_build_id, second_frozen = snapshot(client, job, installation)
    assert second_frozen == frozen and second_build_id != first_build_id
    assert client.put(f"/api/agent/jobs/{job.id}/stand/artifacts/allure-report.html", content=b"report").status_code == 204
    assert client.post(f"/api/agent/jobs/{job.id}/stand/finish", json=completion(job)).status_code == 200
    as_user(client)
    builds = client.get(f"/api/projects/ONE/stand-instances/{instance['id']}/builds").json()
    assert {build["id"] for build in builds} == {first_build_id, second_build_id}
    assert session.get(StandInstance, instance["id"]).deployed_build_id == second_build_id
    assert session.get(Job, original.id).spec == expected


def test_restart_waits_for_existing_lock_while_logs_can_run(client, session, installation):
    instance, original, _, _, _, secret = deployed(client, session, installation)
    blocker = Job(project="ONE", kind="run", target=instance["id"], title="run", status="running",
        created_by="admin", created=auth.now(), heartbeat=auth.now(), spec={"stand_target": {"host_id": "local"}})
    session.add(blocker)
    session.commit()
    restart = client.post(action_url(instance, "restart"), json={}).json()["id"]
    logs = client.post(action_url(instance, "logs"), json={}).json()["id"]
    result = control_claim(client, secret)
    assert result.status_code == 200 and result.json()["id"] == logs
    assert session.get(Job, restart).status == "queued"
    assert client.post(f"/api/agent/jobs/{logs}/finish", json={"status": "done"}).status_code == 200
    blocker.status, blocker.finished = "done", auth.now()
    session.commit()
    assert control_claim(client, secret).json()["id"] == restart
    as_user(client)
    assert client.delete(f"/api/projects/ONE/stand-instances/{instance['id']}").status_code == 409


def test_controls_unavailable_during_deletion(client, session, installation):
    instance, job, _, _, _, secret = deployed(client, session, installation)
    response = client.delete(f"/api/projects/ONE/stand-instances/{instance['id']}")
    assert response.status_code == 202, response.text
    for action in ("deploy", "restart", "status", "logs"):
        assert client.post(action_url(instance, action), json={}).status_code == 409
    stand = client.get("/api/projects/ONE/stands").json()[0]
    assert all(not action["allowed"] for action in stand["actions"])


def test_controls_wait_for_first_snapshot(client, session, installation):
    instance, _, _ = prepare(client, session, installation)
    as_user(client)
    for action in ("deploy", "restart", "status", "logs"):
        result = client.post(action_url(instance, action), json={})
        assert result.status_code == 409
    assert all(not action["allowed"] for action in client.get("/api/projects/ONE/stands").json()[0]["actions"])


def test_queued_logs_are_not_started_after_deletion_is_requested(client, session, installation):
    instance, _, _, _, _, secret = deployed(client, session, installation)
    logs = client.post(action_url(instance, "logs"), json={}).json()["id"]
    assert client.delete(f"/api/projects/ONE/stand-instances/{instance['id']}").status_code == 202
    assert control_claim(client, secret).status_code == 204
    assert session.get(Job, logs).status == "cancelled"


def test_control_report_requires_the_current_host_binding(client, session, installation):
    instance, _, _, _, token, secret = deployed(client, session, installation)
    logs = client.post(action_url(instance, "logs"), json={}).json()["id"]
    assert control_claim(client, secret).json()["id"] == logs
    token.stand_host = "remote"
    session.commit()
    assert client.post(f"/api/agent/jobs/{logs}/log", json={"text": "output"}).status_code == 403
    assert client.post(f"/api/agent/jobs/{logs}/finish", json={"status": "done"}).status_code == 403
    token.stand_host = "local"
    session.commit()
    assert client.post(f"/api/agent/jobs/{logs}/finish", json={"status": "done"}).status_code == 200


@pytest.mark.parametrize("role,owned,can_manage", [("admin", False, True), ("tester", False, True),
    ("developer", False, False), ("developer", True, True), ("viewer", True, False)])
def test_action_availability_uses_managed_owner_and_legacy_read_roles(client, session, installation, role, owned, can_manage):
    instance, _, _, _, _, _ = deployed(client, session, installation)
    if owned:
        session.get(StandInstance, instance["id"]).owner_login = role
        session.commit()
    as_user(client, role)
    stand = client.get("/api/projects/ONE/stands").json()[0]
    allowed = {a["name"] for a in stand["actions"] if a["allowed"]}
    assert ("restart" in allowed) == can_manage
    assert ("deploy" in allowed) == can_manage
    assert ("status" in allowed) == (role != "viewer")
    result = client.post(action_url(instance, "restart"), json={})
    assert result.status_code == (201 if can_manage else 403)
