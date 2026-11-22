import subprocess
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from portal.models import (
    ApiToken, Bug, DataBinding, DataOperation, Dataset, DatasetVersion, Job, JobLog,
    LoginSession, Project, Run, StandBuild, StandInstance, StandPortReservation,
    StandRepository, StandTemplate, TestResult, User,
)

BACKEND = Path(__file__).parent.parent


def test_migrations_apply_and_match_models(tmp_path, monkeypatch):
    """Остальные тесты создают схему из моделей и ошибку в миграции не увидят."""
    monkeypatch.setenv("PORTAL_DATABASE_URL", f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    for command in (["upgrade", "head"], ["check"]):
        done = subprocess.run(
            [sys.executable, "-m", "alembic", *command], cwd=BACKEND, capture_output=True, text=True, encoding="utf-8",
        )
        assert done.returncode == 0, done.stderr


def test_password_suggestion_migration_preserves_existing_accounts(tmp_path, monkeypatch):
    database = tmp_path / "existing.db"
    monkeypatch.setenv("PORTAL_DATABASE_URL", f"sqlite:///{database.as_posix()}")

    def upgrade(revision):
        done = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision], cwd=BACKEND,
            capture_output=True, text=True, encoding="utf-8",
        )
        assert done.returncode == 0, done.stderr

    upgrade("0007")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "INSERT INTO users (login, name, role, password_hash, active, must_change_password, failed_logins, locked_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("admin", "Existing owner", "admin", "existing-password-hash", 0, 1, 3, "2026-11-19 12:00:00"),
        )
        connection.execute(
            "INSERT INTO sessions (token_hash, login, expires) VALUES (?, ?, ?)",
            ("existing-session-hash", "admin", "2026-11-20 12:00:00"),
        )
        before = dict(connection.execute("SELECT * FROM users").fetchone())
        sessions = [tuple(row) for row in connection.execute("SELECT * FROM sessions")]
    upgrade("head")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        after = dict(connection.execute("SELECT * FROM users").fetchone())
        assert after.pop("password_change_suggested") == 0
        assert after == before
        assert [tuple(row) for row in connection.execute("SELECT * FROM sessions")] == sessions


def test_stand_migration_roundtrip_preserves_all_existing_tables(tmp_path, monkeypatch):
    database = tmp_path / "existing-0008.db"
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PORTAL_DATABASE_URL", url)

    def migrate(*command):
        done = subprocess.run(
            [sys.executable, "-m", "alembic", *command], cwd=BACKEND,
            capture_output=True, text=True, encoding="utf-8",
        )
        assert done.returncode == 0, done.stderr

    def contents():
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
            tables = connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name",
            ).fetchall()
            # Имена получены из созданной тестовой БД, не из пользовательского ввода.
            result = {name: (sql, connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall())
                      for name, sql in tables}
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            return result

    migrate("upgrade", "0008")
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    created = datetime(2026, 11, 18, 12)
    try:
        with Session(engine) as session:
            session.execute(Project.__table__.insert().values(code="ONE", title="Existing project"))
            session.add(User(login="owner", name="Existing owner", role="admin", password_hash="old-test-hash",
                             failed_logins=3, password_change_suggested=True))
            session.commit()
            session.execute(ApiToken.__table__.insert().values(
                token_hash="old-token-hash", name="Old token", role="viewer", active=True))
            session.add_all([
                LoginSession(token_hash="old-session-hash", login="owner", expires=datetime(2026, 11, 20)),
                Run(id="run-one", project="ONE", suite="smoke", started=created,
                    path="runs/run-one", source_hash="a" * 64),
                Bug(id="BUG-ONE-0001", project="ONE", number=1, title="Existing bug", source_hash="b" * 64),
                Job(id=1, project="ONE", kind="test", status="done", target="legacy-stand", title="Existing job",
                    spec={"legacy": True}, created_by="owner", created=created),
                Dataset(id=1, project="ONE", key="sample", title="Existing dataset", format="json", version=1,
                        created_by="owner", created=created),
            ])
            session.commit()
            session.add_all([
                TestResult(run_id="run-one", position=0, path="test-case", name="Existing test", verdict="passed"),
                JobLog(job_id=1, text="Existing job log"),
                DatasetVersion(dataset_id=1, version=1, data={"sample": [1, 2]}, created_by="owner", created=created),
                DataOperation(job_id=1, position=0, request={"legacy": True}, adapter={"name": "test"},
                              result={"done": True}),
            ])
            session.commit()
            session.add(DataBinding(project="ONE", stand="legacy-stand", dataset_id=1, version=1, target="sample"))
            session.commit()
        before = contents()
        assert all(rows for _, rows in before.values())
        migrate("upgrade", "head")
        after = contents()

        def assert_old_tables_preserved(current):
            assert {name: current[name] for name in before if name not in ("projects", "api_tokens")} == {
                name: value for name, value in before.items() if name not in ("projects", "api_tokens")
            }
            assert [row[:2] for row in current["projects"][1]] == before["projects"][1]
            assert all(row[2:] == (None, None) for row in current["projects"][1])
            # Единственное ожидаемое изменение DDL старых таблиц — nullable JSON.
            assert current["projects"][0].replace(", configuration JSON", "").replace(", stand_data_bundle JSON", "") == before["projects"][0]
            assert [row[:-2] for row in current["api_tokens"][1]] == before["api_tokens"][1]
            assert all(row[-2:] == (None, None) for row in current["api_tokens"][1])
            assert current["api_tokens"][0].replace(", stand_host VARCHAR(128)", "").replace(
                ", last_seen DATETIME", "") == before["api_tokens"][0]

        assert_old_tables_preserved(after)
        added = {"git_servers", "stand_repositories", "stand_templates", "stand_instances", "stand_builds", "stand_port_reservations"}
        assert set(after) - set(before) == added
        assert all(after[name][1] == [] for name in added)  # автоматического импорта YAML нет
        with Session(engine) as session:
            session.add(StandRepository(project="ONE", key="source", title="Source", components=["api"]))
            session.add(StandTemplate(project="ONE", key="http", version="1", title="HTTP", spec={"schema_version": 1}))
            session.commit()
            session.add(StandInstance(id="stand-one", project="ONE", name="feature", template_key="http",
                                      template_version="1", owner_login="owner", target_host="host-one", profile="laptop"))
            session.commit()
            session.add(StandBuild(id="build-one", project="ONE", stand_id="stand-one", template_key="http",
                                  template_version="1", snapshot={"components": [{"sha": "a" * 40}]}, job_id=1))
            session.add(StandPortReservation(stand_id="stand-one", target_host="host-one", protocol="tcp", port=8080))
            session.commit()
        migrate("check")
        migrate("downgrade", "0008")
        assert contents() == before
        migrate("upgrade", "head")
        migrate("check")
        restored = contents()
        assert_old_tables_preserved(restored)
        assert all(restored[name][1] == [] for name in added)
    finally:
        engine.dispose()
