"""Одноразовая проверка claim на ПУСТОЙ изолированной PostgreSQL БД.

Запуск из backend с PYTHONPATH=backend и PROBE_DATABASE_NAME=portal_batch_probe_20261112.
PORTAL_DATABASE_URL используется только как источник подключения; имя БД заменяется ДО импорта portal.
Скрипт не создаёт/не удаляет БД и никогда не печатает URL подключения.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ALLOWED_DATABASE = "portal_batch_probe_20261112"
assert os.environ.get("PROBE_DATABASE_NAME") == ALLOWED_DATABASE, "нужно явное имя изолированной БД"
source_url = make_url(os.environ["PORTAL_DATABASE_URL"])
assert source_url.get_backend_name() == "postgresql", "probe требует PostgreSQL"
assert source_url.database != ALLOWED_DATABASE, "источник не должен уже указывать на probe БД"
probe_url = source_url.set(database=ALLOWED_DATABASE)
os.environ["PORTAL_DATABASE_URL"] = probe_url.render_as_string(hide_password=False)

# Только после замены URL: portal.db создаёт engine при импорте.
from alembic import command
from alembic.config import Config
from fastapi import Response
from portal import jobs
from portal.auth import AGENT, Principal
from portal.config import ProjectConfig, StandConfig
from portal.models import Job, Project


def main():
    engine = create_engine(probe_url)
    with engine.connect() as connection:
        assert connection.scalar(text("select current_database()")) == ALLOWED_DATABASE
        assert inspect(connection).get_table_names() == [], "probe разрешён только на пустой БД"
        assert connection.get_isolation_level() == "READ COMMITTED", "claim требует READ COMMITTED"
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    command.upgrade(config, "head")
    with TemporaryDirectory(prefix="portal-batch-probe-") as directory:
        project = ProjectConfig(
            code="PROBE", title="Probe", results=Path(directory), environments=["dev", "main"],
            stands=[StandConfig(name="one", title="One", environments=["dev"]),
                    StandConfig(name="two", title="Two", environments=["main"])],
        )
        jobs.load_projects = lambda: [project]
        with Session(engine, expire_on_commit=False) as session:
            session.add(Project(code=project.code, title=project.title))
            session.flush()
            first = jobs.queue_run(session, project, "suites/one.yaml", "dev", "main", "probe", batch_id="probe")
            second = jobs.queue_run(session, project, "suites/two.yaml", "dev", "main", "probe", batch_id="probe")
            session.commit()
            first_id, second_id = first.id, second.id

        entered = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        reads = 0
        original_fail_lost = jobs._fail_lost_jobs

        def paused_fail_lost(session):
            nonlocal reads
            with guard:
                reads += 1
                first_read = reads == 1
            if first_read:
                entered.set()
                assert release.wait(10), "probe не освободил первую выдачу"
            original_fail_lost(session)

        jobs._fail_lost_jobs = paused_fail_lost

        def claim(name):
            worker_engine = create_engine(probe_url, connect_args={"application_name": name})
            try:
                with Session(worker_engine, expire_on_commit=False) as session:
                    response = Response()
                    result = jobs.claim(jobs.ClaimIn(capabilities=["newman"]), response,
                                        Principal(name, AGENT), session)
                    return result.id if result else None
            finally:
                worker_engine.dispose()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_claim = pool.submit(claim, "portal-batch-probe-a")
                try:
                    assert entered.wait(5), "первый claim не дошёл до чтения"
                    second_claim = pool.submit(claim, "portal-batch-probe-b")
                    deadline = time.monotonic() + 5
                    saw_wait = False
                    while time.monotonic() < deadline:
                        with engine.connect() as connection:
                            saw_wait = bool(connection.scalar(text(
                                "select exists(select 1 from pg_stat_activity "
                                "where datname = current_database() "
                                "and application_name = 'portal-batch-probe-b' "
                                "and wait_event = 'advisory')")))
                        if saw_wait:
                            break
                        time.sleep(0.02)
                    assert saw_wait, "второй claim должен ждать PostgreSQL advisory lock"
                    with guard:
                        assert reads == 1, "чтение lost/running произошло до блокировки"
                finally:
                    release.set()
                assert [first_claim.result(timeout=10), second_claim.result(timeout=10)] == [first_id, None]
        finally:
            release.set()
            jobs._fail_lost_jobs = original_fail_lost

        with Session(engine, expire_on_commit=False) as session:
            assert session.scalar(select(func.count()).select_from(Job).where(Job.status == "running")) == 1
            assert session.get(Job, second_id).status == "queued"
            other = jobs.queue_run(session, project, "suites/other.yaml", "main", "main", "probe")
            session.commit()
            other_id = other.id
        assert claim("portal-batch-probe-c") == other_id, "другой стенд должен получить задачу"
        with Session(engine, expire_on_commit=False) as session:
            jobs.finish_job(first_id, jobs.FinishIn(status="done"), Principal("portal-batch-probe-a", AGENT), session)
        assert claim("portal-batch-probe-d") == second_id, "очередь первого стенда должна продолжиться"
    engine.dispose()
    print("PASS: migrations 0006, PostgreSQL advisory contention before reads, one claim per stand, other stand available, FIFO continues")


if __name__ == "__main__":
    main()
