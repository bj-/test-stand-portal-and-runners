import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portal.auth import Principal, visitor
from portal.db import get_session
from portal.main import app
from portal.models import Base, Project


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    configure = tmp_path / "projects.yaml"
    configure.write_text(yaml.safe_dump({"projects": {
        "ACME": {"title": "Acme", "results": "acme"},
        "OTHER": {"title": "Other", "results": "other"},
    }}), encoding="utf-8")
    monkeypatch.setenv("PORTAL_PROJECTS", str(configure))
    monkeypatch.setenv("PORTAL_ARTIFACTS", str(tmp_path / "artifacts"))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(code="LAB", title="Lab"))
        session.commit()
        app.dependency_overrides[get_session] = lambda: session
        try:
            with TestClient(app) as client:
                yield client, tmp_path
        finally:
            app.dependency_overrides.pop(visitor, None)
            app.dependency_overrides.pop(get_session)
    engine.dispose()


def bundle(root: Path, identifier: str, *, slides: bool = True, **metadata) -> Path:
    directory = root / identifier
    directory.mkdir(parents=True)
    (directory / "summary.json").write_text(json.dumps({
        "schema_version": 1,
        "title": "Сводка тестов",
        "date": "2026-11-27",
        "module": "Checkout",
        "description": "Два сценария",
        **metadata,
    }), encoding="utf-8")
    (directory / "report.md").write_text("# Сводка\n\nДва сценария\n", encoding="utf-8")
    if slides:
        (directory / "slides.html").write_text("<!doctype html><title>Сводка</title>", encoding="utf-8")
    return directory


def test_project_scope_list_and_sorting(catalog):
    client, tmp = catalog
    root = tmp / "acme" / "summaries"
    bundle(root, "b-old", date="2026-10-01")
    bundle(root, "new", slides=False, module="")
    bundle(tmp / "other" / "summaries", "unrelated")
    result = client.get("/api/projects/ACME/summaries")
    assert result.status_code == 200
    assert [(entry["id"], entry["has_presentation"]) for entry in result.json()] == [
        ("new", False), ("b-old", True),
    ]
    assert result.json()[0]["date"] == "2026-11-27"
    assert client.get("/api/projects/OTHER/summaries/b-old").status_code == 404
    assert client.get("/api/projects/UNKNOWN/summaries").status_code == 404


def test_detail_and_downloads_for_guest(catalog):
    client, tmp = catalog
    folder = bundle(tmp / "acme" / "summaries", "snapshot")
    url = "/api/projects/ACME/summaries/snapshot"
    detail = client.get(url)
    assert detail.status_code == 200
    assert detail.json()["title"] == "Сводка тестов" and "Два сценария" in detail.json()["markdown"]
    report = client.get(f"{url}/report")
    assert report.text == detail.json()["markdown"]
    assert report.headers["content-type"].startswith("text/markdown")
    assert report.headers["content-disposition"] == 'attachment; filename="snapshot.md"'
    slides = client.get(f"{url}/presentation")
    assert slides.content == (folder / "slides.html").read_bytes()
    assert slides.headers["content-disposition"] == 'attachment; filename="snapshot-slides.html"'
    assert "sandbox" in slides.headers["content-security-policy"]
    assert slides.headers["x-content-type-options"] == "nosniff"


def test_llm_gets_filtered_markdown_and_no_html(catalog):
    client, tmp = catalog
    directory = bundle(tmp / "acme" / "summaries", "snapshot",
                       description="Открыто <!-- ai-restricted:begin --> закрыто <!-- ai-restricted:end -->")
    (directory / "report.md").write_text("Видно\n<!-- ai-restricted:begin -->\nСкрыто\n<!-- ai-restricted:end -->",
                                          encoding="utf-8")
    app.dependency_overrides[visitor] = lambda: Principal("summary-reader", "llm")
    url = "/api/projects/ACME/summaries/snapshot"
    assert "закрыто" not in client.get("/api/projects/ACME/summaries").json()[0]["description"]
    assert "Скрыто" not in client.get(url).json()["markdown"]
    assert "Скрыто" not in client.get(f"{url}/report").text
    assert client.get(f"{url}/presentation").status_code == 403
    app.dependency_overrides[visitor] = lambda: Principal("runner", "agent")
    assert client.get("/api/projects/ACME/summaries").status_code == 403


def test_empty_incomplete_and_optional_deck(catalog):
    client, tmp = catalog
    root = tmp / "acme" / "summaries"
    url = "/api/projects/ACME/summaries"
    assert client.get(url).json() == []
    empty = root / "legacy"
    empty.mkdir(parents=True)
    (empty / "report.md").write_text("Старый отчет", encoding="utf-8")
    bundle(root, "future", schema_version=2)
    bundle(root, "invalid-date", date="2026-02-30")
    bundle(root, "no-report").joinpath("report.md").unlink()
    bundle(root, "snapshot", slides=False)
    assert [entry["id"] for entry in client.get(url).json()] == ["snapshot"]
    assert client.get(f"{url}/legacy").status_code == 404
    assert client.get(f"{url}/snapshot/presentation").status_code == 404


def test_publication_for_projects_without_results_clones(catalog):
    client, tmp = catalog
    artifacts = tmp / "artifacts" / "project-summaries"
    bundle(artifacts / "LAB", "manual", title="Ручная сводка")
    result = client.get("/api/projects/LAB/summaries")
    assert [entry["title"] for entry in result.json()] == ["Ручная сводка"]
    assert client.get("/api/projects/LAB/summaries/manual").json()["project"] == "LAB"
    assert client.get("/api/projects/ACME/summaries/manual").status_code == 404
    bundle(artifacts / "ACME", "duplicate", title="Из артефактов")
    bundle(tmp / "acme" / "summaries", "duplicate", title="Из результатов")
    assert [entry["title"] for entry in client.get("/api/projects/ACME/summaries").json()] == ["Из результатов"]
    assert client.get("/api/projects/ACME/summaries/duplicate").json()["title"] == "Из результатов"


def test_aliases_are_not_sources(catalog):
    client, tmp = catalog
    target = bundle(tmp / "other" / "summaries", "unrelated")
    root = tmp / "acme" / "summaries"
    published = bundle(root, "snapshot", slides=False)
    try:
        (root / "alias").symlink_to(target, target_is_directory=True)
        (published / "slides.html").symlink_to(target / "slides.html")
    except OSError as error:
        pytest.skip(f"Создание ссылок недоступно: {type(error).__name__}")
    url = "/api/projects/ACME/summaries"
    assert [entry["id"] for entry in client.get(url).json()] == ["snapshot"]
    assert client.get(f"{url}/alias").status_code == 404
    assert client.get(f"{url}/snapshot/presentation").status_code == 404
