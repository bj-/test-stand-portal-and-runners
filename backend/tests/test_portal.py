import json
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portal.auth import Principal, current_user, visitor
from portal.config import ProjectConfig
from portal.db import get_session
from portal.importer import parse_bug, split_restricted, sync_project
from portal.main import app
from portal.models import Base, Bug, Run

BUG = """# BUG-ACME-0007. POST /orders: 500 на пустом теле - ошибка сервера вместо 400

| | |
|---|---|
| Модуль | Orders |
| Тип | implementation |
| Серьёзность | major (на prod - critical) |
| Приоритет | <ставит команда> |
| Статус | new |
| Группа | sg-a, sg-b |
| Стенд | http://stand:1 |
| Найден | 2026-01-02, прогон [ACME-20260102-100000-full](../runs/2026/01/ACME-20260102-100000-full/report.md) |
| Трекер | - |

## Описание

Публичная часть.

<!-- ai-restricted:begin -->
Закрытая часть.
<!-- ai-restricted:end -->
"""


def write_run(root: Path, run_id: str, started: str, verdicts: dict[str, str], stand_kind: str = "nonprod") -> None:
    directory = root / "runs" / "2026" / "01" / run_id
    directory.mkdir(parents=True)
    passed = sum(v == "passed" for v in verdicts.values())
    failed = sum(v == "failed" for v in verdicts.values())
    (directory / "run.yaml").write_text(
        yaml.safe_dump({
            "id": run_id, "project": "ACME", "modules": ["Orders"], "kind": "regression",
            "suite": "suites/Orders/full.yaml", "started": started, "finished": started, "who": "tester",
            "trigger": {"type": "manual"}, "gate_failed": False,
            "stand": {"environment": "dev", "url": "http://stand:1", "kind": stand_kind},
            "product": {"build": "unknown"}, "tests": {"branch": "main", "commit": "abc"},
            "engine": {"version": "1.0"}, "runner": "newman",
            "summary": {"tests": len(verdicts), "passed": passed, "failed": failed, "checks": 9, "checks_failed": failed},
        }, allow_unicode=True),
        encoding="utf-8",
    )
    executions = [
        {"path": path, "name": path, "method": "GET", "url": "http://stand:1/x", "code": 200, "time_ms": 5,
         "verdict": verdict, "gate": False, "response_body": "секрет ответа"}
        for path, verdict in verdicts.items()
    ]
    (directory / "results.json").write_text(json.dumps({"executions": executions}), encoding="utf-8")
    (directory / "report.md").write_text("# Отчёт\n\nBUG-ACME-0007\n", encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> ProjectConfig:
    write_run(tmp_path, "ACME-20260102-100000-full", "2026-01-02T10:00:00", {"a": "passed", "b": "failed", "c": "failed"})
    write_run(tmp_path, "ACME-20260103-100000-full", "2026-01-03T10:00:00", {"a": "failed", "b": "passed", "c": "failed"})
    write_run(tmp_path, "ACME-20260104-100000-full", "2026-01-04T10:00:00", {"a": "passed", "b": "failed", "c": "failed"})
    write_run(tmp_path, "ACME-20260105-100000-fix", "2026-01-05T10:00:00", {"a": "failed"}, stand_kind="ephemeral")
    (tmp_path / "bugs").mkdir()
    (tmp_path / "bugs" / "BUG-ACME-0007-заказ.md").write_text(BUG, encoding="utf-8")
    return ProjectConfig("ACME", "Acme", tmp_path)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture
def client(session, project, tmp_path, monkeypatch):
    file = tmp_path / "projects.yaml"
    file.write_text(yaml.safe_dump({"projects": {"ACME": {"title": "Acme", "results": "."}}}), encoding="utf-8")
    monkeypatch.setitem(os.environ, "PORTAL_PROJECTS", str(file))
    sync_project(session, project)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[visitor] = lambda: Principal("tester", "tester")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_sync_is_idempotent(session, project):
    first = sync_project(session, project)
    assert (first.runs_added, first.bugs_added) == (4, 1)
    second = sync_project(session, project)
    assert (second.runs_added, second.runs_updated, second.runs_skipped) == (0, 0, 4)
    assert (second.bugs_added, second.bugs_updated, second.bugs_skipped) == (0, 0, 1)


def test_sync_updates_changed_run_and_removes_deleted(session, project):
    sync_project(session, project)
    run_dir = project.results / "runs/2026/01/ACME-20260102-100000-full"
    (run_dir / "report.md").write_text("разбор дописан BUG-ACME-0009", encoding="utf-8")
    stats = sync_project(session, project)
    assert stats.runs_updated == 1
    assert session.get(Run, "ACME-20260102-100000-full").bug_ids == ["BUG-ACME-0009"]

    deleted = project.results / "runs/2026/01/ACME-20260103-100000-full"
    for file in deleted.iterdir():
        file.unlink()
    deleted.rmdir()
    sync_project(session, project)
    assert session.get(Run, "ACME-20260103-100000-full") is None


def test_parse_bug_header():
    bug = parse_bug(BUG)
    assert bug["id"] == "BUG-ACME-0007" and bug["number"] == 7
    assert bug["severity"] == "major"
    assert bug["groups"] == ["sg-a", "sg-b"]
    assert bug["found_date"] == "2026-01-02"
    assert bug["run_id"] == "ACME-20260102-100000-full"
    assert bug["tracker"] is None


def test_restricted_part_is_separated():
    public, closed = split_restricted(BUG)
    assert "Закрытая часть" not in public and "Публичная часть" in public
    assert "Закрытая часть" in closed


def test_api_never_returns_restricted_body(client, session):
    body = client.get("/api/bugs/BUG-ACME-0007").text
    assert "Закрытая часть" not in body and "restricted_body" not in body
    assert "Закрытая часть" in session.get(Bug, "BUG-ACME-0007").restricted_body


def test_runs_list_and_detail(client):
    page = client.get("/api/runs", params={"project": "ACME"}).json()
    assert page["total"] == 4
    assert page["items"][0]["id"] == "ACME-20260105-100000-fix"

    run_id = "ACME-20260102-100000-full"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["failed"] == 2 and detail["has_report"] and not detail["has_allure"]
    assert detail["bug_ids"] == ["BUG-ACME-0007"]
    assert [r["path"] for r in detail["results"]] == ["a", "b", "c"]
    assert "секрет ответа" not in json.dumps(detail, ensure_ascii=False)
    assert client.get(f"/api/runs/{run_id}/report").text.startswith("# Отчёт")
    assert client.get(f"/api/runs/{run_id}/allure").status_code == 404
    assert client.get("/api/runs/нет").status_code == 404


def test_compare_runs(client):
    result = client.get("/api/runs/ACME-20260102-100000-full/compare/ACME-20260103-100000-full").json()
    assert result["new_failures"] == ["a"]
    assert result["fixed"] == ["b"]
    assert result["still_failing"] == ["c"]


def test_stats_exclude_ephemeral_stands(client):
    stats = client.get("/api/stats", params={"project": "ACME"}).json()
    assert stats["runs"] == 3
    assert stats["tests_executed"] == 9
    assert stats["pass_rate"] == pytest.approx(3 / 9, abs=0.001)
    assert stats["top_failing"][0] == {"path": "c", "failed": 3, "runs": 3}
    assert {t["path"] for t in stats["unstable"]} == {"a", "b"}
    assert stats["bugs_by_severity"] == [{"name": "major", "count": 1}]


def test_llm_gets_report_without_restricted_blocks(client, project):
    run_id = "ACME-20260102-100000-full"
    report = project.results / "runs" / "2026" / "01" / run_id / "report.md"
    report.write_text("# Отчёт\n\nоткрыто\n<!-- ai-restricted:begin -->\nтокен abc\n<!-- ai-restricted:end -->\nконец\n",
                      encoding="utf-8")
    assert "токен abc" in client.get(f"/api/runs/{run_id}/report").text  # человек видит всё
    app.dependency_overrides[visitor] = lambda: Principal("session-1", "llm")
    text = client.get(f"/api/runs/{run_id}/report").text
    assert "токен abc" not in text and "[скрыто: ai-restricted" in text and "открыто" in text and "конец" in text
    assert client.get(f"/api/runs/{run_id}/allure").status_code == 403


def test_restricted_dirs_are_detected():
    from portal import ai_guard
    assert ai_guard.in_restricted_dir("runs/2026/01/X/groups/sg-restricted")
    assert ai_guard.in_restricted_dir("bugs/restricted/BUG-1.md")
    assert not ai_guard.in_restricted_dir("runs/2026/11/BFF-20261105-124909-security-restricted")


def test_guest_invalid_cookie_is_cleared_for_file_response(client, monkeypatch, tmp_path):
    from portal import main
    from portal.auth import COOKIE
    app.dependency_overrides.pop(visitor)
    file = tmp_path / "allure-report.html"
    file.write_text("<html>local report</html>", encoding="utf-8")
    monkeypatch.setattr(main, "_allure_file", lambda session, run: file)
    client.cookies.set(COOKIE, "invalid-session")
    response = client.get("/api/runs/ACME-20260102-100000-full/allure")
    assert response.status_code == 200 and response.text == "<html>local report</html>"
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_guest_and_viewer_receive_same_subject_data(client):
    paths = ["/api/projects", "/api/runs", "/api/bugs", "/api/stats",
             "/api/runs/ACME-20260102-100000-full", "/api/runs/ACME-20260102-100000-full/report"]
    viewer_data = [(client.get(path).status_code, client.get(path).content) for path in paths]
    app.dependency_overrides.pop(visitor)
    guest_data = [(client.get(path).status_code, client.get(path).content) for path in paths]
    assert guest_data == viewer_data
