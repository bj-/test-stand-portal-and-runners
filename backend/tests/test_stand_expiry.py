import asyncio
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from portal import auth, stand_expiry
from portal.models import Job, StandBuild, StandInstance, StandPortReservation
from test_managed_stands import body, client, create, installation, register, session
from test_stand_deletion import claim_delete, delete_url, finished_instance, private_artifacts
from test_stand_execution import as_user


def test_scheduler_checks_once_per_day_at_local_midnight(monkeypatch):
    moment = datetime(2026, 11, 24, 15, 30)
    checks = []
    stop = asyncio.Event()

    class Clock(datetime):
        @classmethod
        def now(cls):
            return moment

    async def advance_clock(signal, timeout):
        nonlocal moment
        if stop.is_set():
            await signal
            return
        signal.close()
        moment = datetime.fromtimestamp(moment.timestamp() + timeout)
        raise TimeoutError

    async def run_check(_check):
        nonlocal moment
        checks.append(moment)
        moment += timedelta(seconds=20)
        if len(checks) == 2:
            stop.set()

    monkeypatch.setattr(stand_expiry, "datetime", Clock)
    monkeypatch.setattr(stand_expiry, "asyncio", SimpleNamespace(wait_for=advance_clock, to_thread=run_check))
    asyncio.run(stand_expiry.run(stop))

    assert checks == [datetime(2026, 11, 25), datetime(2026, 11, 26)]


@pytest.mark.parametrize(("offset", "utc_midnight_offset"), [("+03:00", -3), ("-08:00", 8)])
def test_new_stand_uses_midnight_in_the_requested_offset(client, session, offset, utc_midnight_offset):
    register(client)
    day = datetime.now(UTC).date() + timedelta(days=2)
    payload = body(mode="ephemeral", expires=f"{day}T01:00:00{offset}")
    response = create(client, payload)
    assert response.status_code == 201, response.text
    instance = response.json()
    row = session.get(StandInstance, instance["id"])
    deadline = datetime.combine(day + timedelta(days=1), time()) + timedelta(hours=utc_midnight_offset)
    assert row.delete_after == deadline
    assert datetime.fromisoformat(instance["expires"]) == datetime.fromisoformat(payload["expires"])
    assert create(client, payload).status_code == 200
    same_instant = datetime.fromisoformat(payload["expires"]).astimezone(UTC).isoformat()
    assert create(client, {**payload, "expires": same_instant}).status_code == 409
    session.get(Job, instance["job_id"]).status = "cancelled"
    session.commit()

    assert stand_expiry.queue_expired(session, deadline - timedelta(microseconds=1)) == 0
    assert stand_expiry.queue_expired(session, deadline) == 1
    assert stand_expiry.queue_expired(session, deadline + timedelta(minutes=1)) == 0
    detail = client.get(delete_url(instance)).json()
    assert detail["status"] == "deleting" and detail["deletion_status"] == "queued"
    job = session.get(Job, detail["deletion_job_id"])
    assert job.created_by == stand_expiry.AUTOMATIC_ACTOR
    assert job.spec["workspace_required"] is False
    assert client.delete(delete_url(instance)).json()["deletion_job_id"] == job.id


def test_legacy_stand_waits_for_active_job_then_uses_normal_host_cleanup(client, session, installation):
    instance, build_id, _, secret = finished_instance(client, session, installation)
    row = session.get(StandInstance, instance["id"])
    expiration_date = datetime.now().date() - timedelta(days=2)
    expiry = datetime.combine(expiration_date, time(12)).astimezone(UTC).replace(tzinfo=None)
    midnight = datetime.combine(expiration_date + timedelta(days=1), time.min)
    deadline = midnight.astimezone(UTC).replace(tzinfo=None)
    row.mode, row.created, row.expires = "ephemeral", expiry - timedelta(days=1), expiry
    work = Job(project="ONE", kind="run", status="running", target="feature", title="tests",
               created_by="admin", created=auth.now(), spec={})
    session.add(work)
    session.commit()

    assert stand_expiry.queue_expired(session, deadline - timedelta(microseconds=1)) == 0
    assert stand_expiry.queue_expired(session, deadline) == 0
    work.status = "done"
    session.commit()
    assert stand_expiry.queue_expired(session, deadline) == 1
    assert row.deleted_at is None and session.scalar(select(StandPortReservation)) is not None
    assert claim_delete(client, secret, host="other").status_code == 204
    claim = claim_delete(client, secret)
    assert claim.status_code == 200
    assert claim.json()["spec"]["workspace_required"] is True
    deletion_id = claim.json()["id"]
    finish = client.post(f"/api/agent/jobs/{deletion_id}/stand/delete-finish", json={"status": "done"})
    assert finish.status_code == 200

    assert stand_expiry.queue_expired(session, deadline + timedelta(days=1)) == 0
    session.refresh(row)
    assert row.deleted_at is not None and session.scalar(select(StandPortReservation)) is None
    assert session.get(StandBuild, build_id) is not None
    as_user(client)
    assert client.get(delete_url(instance)).json()["status"] == "deleted"
    assert client.get(f"/api/stand-builds/{build_id}/artifacts/allure-report.html").content == b"report"


def test_sweep_keeps_permanent_stand_and_manual_deletion_and_does_not_repeat_cancelled_cleanup(client, session):
    register(client)
    permanent = create(client).json()
    moment = auth.now()
    payload = body(name="Temporary", environments=["other"], mode="ephemeral",
                   expires=(moment.replace(tzinfo=UTC) + timedelta(days=1)).isoformat())
    instance = create(client, payload, key="temporary").json()
    session.get(Job, instance["job_id"]).status = "cancelled"
    session.commit()
    later = moment + timedelta(days=3)

    manual_id = client.delete(delete_url(instance)).json()["deletion_job_id"]
    assert stand_expiry.queue_expired(session, later) == 0
    assert client.get(delete_url(instance)).json()["deletion_job_id"] == manual_id
    assert client.post(f"/api/jobs/{manual_id}/cancel").status_code == 200
    assert stand_expiry.queue_expired(session, later) == 1
    automatic_id = client.get(delete_url(instance)).json()["deletion_job_id"]
    assert automatic_id != manual_id
    assert client.post(f"/api/jobs/{automatic_id}/cancel").status_code == 200
    assert stand_expiry.queue_expired(session, later + timedelta(days=1)) == 0
    assert client.get(delete_url(permanent)).json()["deletion_job_id"] is None
    assert client.delete(delete_url(instance)).json()["deletion_job_id"] != automatic_id
