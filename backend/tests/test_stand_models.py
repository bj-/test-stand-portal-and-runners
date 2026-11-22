"""Ограничения БД: одинаковые сценарии на create_all и реальной миграции."""
import copy
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from portal.models import (
    Base, Job, Project, StandBuild, StandInstance, StandPortReservation,
    StandRepository, StandTemplate, User,
)

BACKEND = Path(__file__).parent.parent
CREATED = datetime(2026, 11, 18, 12)


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory):
    database = tmp_path_factory.mktemp("stand-migration") / "schema.db"
    environment = {**os.environ, "PORTAL_DATABASE_URL": f"sqlite:///{database.as_posix()}"}
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND,
        env=environment, capture_output=True, text=True, encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr
    return database


@pytest.fixture(params=["models", "migration"])
def session(request, tmp_path):
    database = tmp_path / "stands.db"
    if request.param == "migration":
        shutil.copyfile(request.getfixturevalue("migrated_database"), database)
    engine = create_engine(f"sqlite:///{database.as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    if request.param == "models":
        Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            assert db.scalar(text("PRAGMA foreign_keys")) == 1
            db.add_all([Project(code=code, title=code) for code in ("ONE", "TWO")])
            db.add(User(login="owner", name="Owner", role="operator", password_hash="test-hash"))
            db.commit()
            db.add_all([
                StandTemplate(project=project, key="http", version=version, title="HTTP", spec={
                    "schema_version": 1, "id": "http", "version": version,
                }, created=CREATED)
                for project, version in (("ONE", "1"), ("ONE", "2"), ("TWO", "1"))
            ])
            db.commit()
            yield db
    finally:
        engine.dispose()


def stand(**overrides):
    values = dict(
        id="stand-one", project="ONE", name="feature", template_key="http", template_version="1",
        owner_login="owner", target_host="host-one", mode="permanent", expires=None,
        profile="laptop", refs={"api": {"repository": "source", "ref": "feature/orders"}},
        parameters={"limit": 10}, resources={"cpus": 1, "memory_mb": 512},
        created=CREATED, updated=CREATED,
    )
    return StandInstance(**(values | overrides))


def build(**overrides):
    snapshot = {
        "schema_version": 1,
        "template": {"schema_version": 1, "id": "http", "version": "1"},
        "template_sha256": "e" * 64,
        "components": [
            {"id": "api", "repository": "source", "requested_ref": "feature/orders", "sha": "a" * 40},
            {"id": "smoke", "repository": "runner", "requested_ref": "test", "sha": "b" * 64},
        ],
        "profile": "laptop", "parameters": {"limit": 10, "label": "Проверка", "enabled": True},
        "secret_refs": [],
    }
    values = dict(
        id="build-one", project="ONE", stand_id="stand-one", template_key="http", template_version="1",
        snapshot=snapshot, created=CREATED,
    )
    return StandBuild(**(values | overrides))


def reservation(**overrides):
    return StandPortReservation(**(dict(
        stand_id="stand-one", target_host="host-one", protocol="tcp", port=8080, bind_address="127.0.0.1",
    ) | overrides))


def rejected(session, row):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(row)
        session.flush()


def test_catalog_and_template_versions_are_project_scoped(session):
    session.add_all([
        StandRepository(project=project, key="source", title="Source", components=["api"])
        for project in ("ONE", "TWO")
    ])
    session.commit()
    assert len(session.scalars(select(StandRepository)).all()) == 2
    assert len(session.scalars(select(StandTemplate)).all()) == 3
    rejected(session, StandRepository(project="ONE", key="source", title="Duplicate"))
    rejected(session, StandRepository(project="MISSING", key="source", title="Unknown project"))
    rejected(session, StandTemplate(project="ONE", key="http", version="1", title="Duplicate", spec={}))
    rejected(session, StandTemplate(project="MISSING", key="http", version="1", title="Missing", spec={}))


@pytest.mark.parametrize("changes", [
    {"template_version": "missing"},
    {"project": "TWO", "template_version": "2"},  # v2 есть только в ONE
    {"project": "MISSING"}, {"owner_login": "missing"},
])
def test_instance_requires_owner_project_and_template_in_same_project(session, changes):
    rejected(session, stand(**changes))


def test_instance_names_and_request_keys_are_scoped_but_ids_are_global(session):
    session.add(stand(request_key="create-request"))
    session.commit()
    rejected(session, stand(id="duplicate-name", request_key="another-request"))
    rejected(session, stand(id="duplicate-request", name="another-name", request_key="create-request"))
    rejected(session, stand(project="TWO"))  # даже в другом проекте ID занят
    session.add_all([
        stand(id="stand-two", project="TWO", request_key="create-request"),
        stand(id="no-request-1", name="first"), stand(id="no-request-2", name="second"),
    ])
    session.commit()
    assert len(session.scalars(select(StandInstance)).all()) == 4


def test_deleted_names_can_be_reused_without_removing_history(session):
    original = stand(deleted_at=CREATED + timedelta(hours=1), request_key="old-request")
    session.add(original)
    session.commit()
    session.add(build())
    session.add(stand(id="replacement", request_key="new-request"))
    session.commit()
    assert session.get(StandBuild, "build-one").stand_id == original.id
    rejected(session, stand(id="still-duplicate", request_key="third-request"))
    rejected(session, stand(id="request-reuse", name="other", request_key="old-request"))


@pytest.mark.parametrize("changes", [
    {"mode": "unknown"}, {"status": "unknown"}, {"id": " "}, {"name": " "},
    {"target_host": " "}, {"request_key": " "},
    {"mode": "permanent", "expires": CREATED + timedelta(days=1)},
    {"mode": "ephemeral", "expires": None},
    {"mode": "ephemeral", "expires": CREATED},
    {"mode": "ephemeral", "expires": CREATED - timedelta(seconds=1)},
])
def test_instance_checks_invalid_identity_state_and_expiry(session, changes):
    rejected(session, stand(**changes))


def test_lifetime_persists_in_new_session_and_expired_rows_remain_valid(session):
    expiry = CREATED + timedelta(hours=3)
    session.add_all([stand(), stand(id="temporary", name="temporary", mode="ephemeral", expires=expiry)])
    session.commit()
    with Session(session.get_bind()) as reconnected:
        assert reconnected.get(StandInstance, "stand-one").expires is None
        temporary = reconnected.get(StandInstance, "temporary")
        assert temporary.mode == "ephemeral" and temporary.expires == expiry
    # Истёкшие строки нужны для остановки/продления; CHECK не зависит от текущих часов.
    session.add(stand(id="expired", name="expired", mode="ephemeral",
                      created=CREATED - timedelta(days=10), expires=CREATED - timedelta(days=9)))
    session.commit()


def test_build_snapshot_and_execution_results_remain_separate(session):
    session.add(stand())
    session.commit()
    first = build()
    original = copy.deepcopy(first.snapshot)
    session.add(first)
    session.commit()
    template = session.get(StandTemplate, ("ONE", "http", "1"))
    template.spec = {"changed": True}
    instance = session.get(StandInstance, "stand-one")
    instance.refs = {"api": {"ref": "next-branch"}}
    instance.template_version = "2"
    first.status = "done"
    first.result = {"healthcheck": "passed", "baseUrl": "http://localhost:8080"}
    first.images = [{"service": "api", "digest": "sha256:" + "c" * 64}]
    first.log_refs = ["builds/build-one/build.log"]
    first.artifact_refs = ["builds/build-one/allure-report.html"]
    session.commit()
    session.expire_all()
    saved = session.get(StandBuild, "build-one")
    assert saved.snapshot == original
    assert [len(c["sha"]) for c in saved.snapshot["components"]] == [40, 64]
    assert saved.template_version == "1" and saved.result["healthcheck"] == "passed"
    assert saved.images == [{"service": "api", "digest": "sha256:" + "c" * 64}]
    assert saved.log_refs == ["builds/build-one/build.log"]
    assert saved.artifact_refs == ["builds/build-one/allure-report.html"]
    assert instance.template_version == "2"


@pytest.mark.parametrize("changes", [
    {"project": "TWO"}, {"stand_id": "missing"}, {"template_key": "missing"},
    {"template_version": "missing"}, {"job_id": 12345}, {"status": "unknown"}, {"id": " "},
])
def test_build_rejects_invalid_links_and_state(session, changes):
    session.add_all([stand(), stand(id="stand-two", name="second", project="TWO")])
    session.commit()
    rejected(session, build(**changes))


def test_build_template_cannot_reference_version_only_in_another_project(session):
    session.add(stand(project="TWO"))
    session.commit()
    rejected(session, build(project="TWO", template_version="2"))


def test_builds_preserve_history_and_optional_job_link(session):
    session.add(stand())
    session.add(Job(project="ONE", kind="build", status="queued", target="stand-one", title="Build",
                    spec={}, created_by="owner", created=CREATED))
    session.commit()
    job = session.scalars(select(Job)).one()
    session.add_all([build(), build(id="build-two", job_id=job.id)])
    session.commit()
    rejected(session, build())
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(delete(StandInstance).where(StandInstance.id == "stand-one"))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(delete(Job).where(Job.id == job.id))
    assert len(session.scalars(select(StandBuild)).all()) == 2


def test_ports_conflict_across_interfaces_and_projects_but_not_hosts_or_protocols(session):
    session.add_all([
        stand(), stand(id="stand-two", project="TWO"),
        stand(id="stand-three", name="remote", target_host="host-two"),
    ])
    session.commit()
    session.add(reservation())
    session.commit()
    rejected(session, reservation(stand_id="stand-two", bind_address="0.0.0.0"))
    rejected(session, reservation(stand_id="stand-two", bind_address="::1"))
    session.add_all([
        reservation(stand_id="stand-two", protocol="udp"),
        reservation(stand_id="stand-three", target_host="host-two"),
        reservation(port=1), reservation(port=65535),
    ])
    session.commit()
    assert len(session.scalars(select(StandPortReservation)).all()) == 5


@pytest.mark.parametrize("changes", [
    {"port": 0}, {"port": 65536}, {"protocol": "TCP"}, {"protocol": "sctp"},
    {"target_host": "host-two"}, {"stand_id": "missing"},
])
def test_port_requires_valid_protocol_range_and_the_instance_host(session, changes):
    session.add_all([stand(), stand(id="remote", name="remote", target_host="host-two")])
    session.commit()
    rejected(session, reservation(**changes))


def test_referenced_stand_host_owner_and_template_cannot_disappear(session):
    session.add(stand())
    session.commit()
    session.add(reservation())
    session.commit()
    for statement in (
        delete(StandInstance).where(StandInstance.id == "stand-one"),
        update(StandInstance).where(StandInstance.id == "stand-one").values(target_host="host-two"),
        delete(User).where(User.login == "owner"),
        delete(StandTemplate).where(StandTemplate.project == "ONE", StandTemplate.version == "1"),
    ):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(statement)
    assert session.scalars(select(StandPortReservation)).one().port == 8080
    # Освобождение должно быть явным, после будущей остановки ресурсов сервисом.
    session.execute(delete(StandPortReservation))
    session.execute(delete(StandInstance).where(StandInstance.id == "stand-one"))
    session.commit()
