"""Переход 0009 ↔ 0010 сохраняет существующие данные и задаёт безопасные defaults."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


BACKEND = Path(__file__).parent.parent


def test_registry_migration_defaults_and_roundtrip_preserve_existing_data(tmp_path):
    database = tmp_path / "registry.db"
    environment = {**os.environ, "PORTAL_DATABASE_URL": f"sqlite:///{database.as_posix()}"}

    def migrate(direction, revision):
        result = subprocess.run([sys.executable, "-m", "alembic", direction, revision],
                                cwd=BACKEND, env=environment, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr

    def snapshot():
        engine = create_engine(f"sqlite:///{database.as_posix()}")
        inspector = inspect(engine)
        # Batch migrations rebuild this table; compare the actual constraints,
        # allowing SQLite to reorder their DDL and quote the table name.
        stand_constraints = {key: sorted(getattr(inspector, key)("stand_instances"), key=repr)
                             for key in ("get_check_constraints", "get_foreign_keys", "get_unique_constraints", "get_indexes")}
        stand_constraints = json.loads(json.dumps(stand_constraints, default=str))
        engine.dispose()
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA foreign_keys=ON")
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            tables = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name").fetchall()
            return {name: {"sql": stand_constraints if name == "stand_instances" else sql,
                           "columns": db.execute(f'PRAGMA table_info("{name}")').fetchall(),
                           "rows": db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()}
                    for name, sql in tables}

    migrate("upgrade", "0009")
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO projects(code,title) VALUES ('ONE','Existing')")
        db.execute("INSERT INTO stand_repositories(project,key,title,components,enabled) "
                   "VALUES ('ONE','source','Source','[\"api\"]',1)")
        db.execute("INSERT INTO users(login,name,role,password_hash,active,must_change_password,failed_logins,"
                   "password_change_suggested) VALUES ('owner','Owner','admin','test-only',1,0,0,1)")
        db.execute("INSERT INTO stand_templates(project,key,version,title,spec,created) "
                   "VALUES ('ONE','http','1','HTTP',?,'2026-11-18 12:00:00')", (json.dumps({"old": "template"}),))
        db.execute("INSERT INTO stand_instances(id,project,name,template_key,template_version,owner_login,target_host,"
                   "mode,profile,refs,parameters,resources,status,created,updated) VALUES "
                   "('stand','ONE','Feature','http','1','owner','host','permanent','test','{}','{}','{}','created',"
                   "'2026-11-18 12:00:00','2026-11-18 12:00:00')")
        db.execute("INSERT INTO stand_builds(id,project,stand_id,template_key,template_version,snapshot,status,images,"
                   "log_refs,artifact_refs,created) VALUES ('build','ONE','stand','http','1',?, 'done','[]','[]','[]',"
                   "'2026-11-18 12:00:00')", (json.dumps({"old": "snapshot"}),))
        db.execute("INSERT INTO stand_port_reservations(id,stand_id,target_host,protocol,port) "
                   "VALUES (1,'stand','host','tcp',8080)")
    before = snapshot()
    migrate("upgrade", "0016")
    before_technical = snapshot()
    migrate("upgrade", "head")
    after = snapshot()
    assert set(after) == set(before) | {"git_servers"}
    assert after["git_servers"]["rows"] == []
    changed = {"projects": ["configuration", "stand_data_bundle"],
               "stand_instances": ["environments", "request_hash", "deployed_build_id", "deleted_at", "delete_after"],
               "stand_templates": ["output_binding_titles", "name_ref_exclusions", "technical_components"],
               "api_tokens": ["stand_host", "last_seen"], "stand_repositories": ["git_url", "git_server_id"]}
    for table, previous in before.items():
        if table not in changed:
            assert after[table] == previous
            continue
        count = len(changed[table])
        assert after[table]["columns"][:-count] == previous["columns"]
        assert [row[:-count] for row in after[table]["rows"]] == previous["rows"]
        assert [column[1] for column in after[table]["columns"][-count:]] == changed[table]
    assert all(column[2:5] == ("JSON", 0, None) for column in after["projects"]["columns"][-2:])
    assert after["projects"]["rows"][0][-2:] == (None, None)
    assert after["stand_instances"]["columns"][-5][2:5] == ("JSON", 1, "'[]'")
    assert json.loads(after["stand_instances"]["rows"][0][-5]) == []
    assert after["stand_instances"]["columns"][-4][2:5] == ("VARCHAR(64)", 0, None)
    assert after["stand_instances"]["columns"][-3][2:5] == ("VARCHAR(128)", 0, None)
    assert all(column[2:5] == ("DATETIME", 0, None) for column in after["stand_instances"]["columns"][-2:])
    assert after["stand_instances"]["rows"][0][-4:] == (None, None, None, None)
    assert all(column[2:5] == ("JSON", 0, None) for column in after["stand_templates"]["columns"][-3:])
    assert after["stand_templates"]["rows"][0][-3:] == (None, None, None)
    assert after["stand_repositories"]["columns"][-2][2:5] == ("VARCHAR(2048)", 0, None)
    assert after["stand_repositories"]["columns"][-1][2:5] == ("VARCHAR(36)", 0, None)
    assert after["stand_repositories"]["rows"][0][-2:] == (None, None)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE stand_templates SET technical_components='[\"api\"]' WHERE project='ONE'")
    migrate("downgrade", "0016")
    assert snapshot() == before_technical
    migrate("upgrade", "head")
    assert snapshot() == after
    with sqlite3.connect(database) as db:
        db.execute("UPDATE stand_instances SET status='limited', deployed_build_id='build' WHERE id='stand'")
    migrate("downgrade", "0017")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT status, deployed_build_id FROM stand_instances WHERE id='stand'").fetchone() == ("failed", "build")
        assert db.execute("SELECT id FROM stand_builds").fetchone() == ("build",)
        db.execute("UPDATE stand_instances SET status='created', deployed_build_id=NULL WHERE id='stand'")
    migrate("upgrade", "head")
    assert snapshot() == after
    with sqlite3.connect(database) as db:
        # 0010 defaults работают и для raw INSERT, без ORM client default.
        db.execute("INSERT INTO projects(code,title) VALUES ('TWO','New index')")
        assert db.execute("SELECT configuration FROM projects WHERE code='TWO'").fetchone() == (None,)
        db.execute("DELETE FROM projects WHERE code='TWO'")
        db.execute("INSERT INTO stand_instances(id,project,name,template_key,template_version,owner_login,target_host,"
                   "mode,profile,refs,parameters,resources,status,created,updated) VALUES "
                   "('second','ONE','Second','http','1','owner','host','permanent','test','{}','{}','{}','created',"
                   "'2026-11-18 12:00:00','2026-11-18 12:00:00')")
        assert db.execute("SELECT environments FROM stand_instances WHERE id='second'").fetchone() == ("[]",)
        db.execute("DELETE FROM stand_instances WHERE id='second'")
        db.execute("UPDATE projects SET configuration=? WHERE code='ONE'", (json.dumps({"runner": "pytest"}),))
        db.execute("UPDATE stand_instances SET environments='[\"explicit\"]' WHERE id='stand'")
        db.execute("UPDATE stand_templates SET name_ref_exclusions='[\"api\"]' WHERE project='ONE'")
    migrate("downgrade", "0009")
    assert snapshot() == before
    migrate("upgrade", "head")
    assert snapshot() == after
    result = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=BACKEND, env=environment,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
