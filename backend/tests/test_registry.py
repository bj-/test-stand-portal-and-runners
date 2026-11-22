"""Контракт объединённого реестра: конфликты, приватность и чтение без записи."""
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from portal.config import load_projects
from portal.models import Base, Project, StandBuild, StandInstance, StandTemplate, User
from portal.registry import RegistryError, load_registry


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(login="owner", name="Owner", role="admin", password_hash="test-only"))
        db.commit()
        yield db
    engine.dispose()


def manifest(tmp_path, projects):
    file = tmp_path / "projects.yaml"
    file.write_text(yaml.safe_dump({"projects": projects}), encoding="utf-8")
    return file


@pytest.mark.parametrize("code", ["ПРОЕКТ", "team space", "team:api"])
def test_existing_project_codes_are_not_limited_to_ascii(session, tmp_path, code):
    session.add(Project(code=code, title="Прежний проект"))
    session.commit()
    file = manifest(tmp_path, {code: {"title": "Настройки YAML", "results": "results"}})
    registry = load_registry(session, file)
    assert registry.get(code).title == "Настройки YAML"
    assert registry.summaries == [{"code": code, "title": "Настройки YAML"}]


def test_database_paths_use_same_canonical_root_as_yaml(session, tmp_path):
    session.add(Project(code="DB", title="DB", configuration={
        "results": str(tmp_path / "unused" / ".." / "results"),
    }))
    session.commit()
    project = load_registry(session, manifest(tmp_path, {})).get("DB")
    assert project.results == (tmp_path / "results").resolve()


def add_stand(session, project, id="instance-id", name="feature", environments=None, **overrides):
    if session.get(StandTemplate, (project, "http", "1")) is None:
        session.add(StandTemplate(project=project, key="http", version="1", title="HTTP", spec={
            "smoke": {"env": "template-env-must-not-appear"}, "private": "template-secret",
        }))
        session.flush()
    stand = StandInstance(
        id=id, name=name, project=project, owner_login="owner", template_key="http", template_version="1",
        target_host="local", profile="test", environments=environments if environments is not None else [],
        refs={"env": "ref-must-not-appear"}, parameters={"secret": "parameter-secret"},
        resources={"private": "resource-secret"}, **overrides,
    )
    session.add(stand)
    session.commit()
    return stand


def test_mixed_registry_preserves_yaml_and_appends_managed_stands(session, tmp_path):
    file = manifest(tmp_path, {"YAML": {
        "title": "YAML title", "results": "results", "tests": "../tests", "environments": ["legacy-env"],
        "stands": {"legacy": {"commands": {"start": "echo legacy"}, "environments": ["legacy-env"]}},
    }})
    session.add_all([Project(code="YAML", title="Indexed title"), Project(code="DB", title="DB title", configuration={
        "results": str(tmp_path / "db-results"), "tests": None, "runner": "pytest", "capability": "phone",
        "tests_url": "https://tests.example", "results_url": "https://results.example", "environments": ["db-env"],
    }), Project(code="HISTORY", title="History")])
    session.commit()
    add_stand(session, "YAML", environments=["feature-env"])
    registry = load_registry(session, file)
    assert registry.errors == {}
    assert list(registry.projects) == ["DB", "HISTORY", "YAML"]
    assert registry.summaries == [{"code": "DB", "title": "DB title"}, {"code": "HISTORY", "title": "History"},
                                  {"code": "YAML", "title": "YAML title"}]
    project = registry.get("YAML")
    assert project.source == "yaml"
    assert project.title == "YAML title"
    assert project.results == (tmp_path / "results").resolve()
    assert project.tests == (tmp_path / "../tests").resolve()
    assert project.stands[0].commands == {"start": "echo legacy"}
    assert [stand.name for stand in project.stands] == ["legacy", "instance-id"]
    assert project.environments == ["legacy-env", "feature-env"]
    assert project.stand_of("feature-env") == "instance-id"
    assert registry.get("DB").source == "database"
    assert registry.get("DB").run_capability == "phone"
    assert registry.get("DB").tests is None
    assert registry.get("HISTORY").source == "index"
    assert registry.get("HISTORY").results is None
    assert asdict(load_projects(file)[0]) == asdict(project) | {
        "environments": ["legacy-env"], "stands": [asdict(project.stands[0])],
    }


def test_db_only_implicit_missing_yaml_and_indexed_managed_stand(session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PORTAL_PROJECTS", raising=False)
    session.add_all([Project(code="DB", title="Configured", configuration={}), Project(code="INDEX", title="History")])
    session.commit()
    add_stand(session, "INDEX", environments=["explicit-env"])
    registry = load_registry(session)
    assert registry.get("DB").results is None
    assert registry.get("INDEX").stand_of("explicit-env") == "instance-id"
    assert registry.get("INDEX").source == "index"
    with pytest.raises(KeyError):
        registry.get("missing")


@pytest.mark.parametrize("via_env", [False, True])
def test_missing_explicit_yaml_is_safe_global_error(session, tmp_path, monkeypatch, via_env):
    private = tmp_path / "private-path-secret.yaml"
    if via_env:
        monkeypatch.setenv("PORTAL_PROJECTS", str(private))
    with pytest.raises(RegistryError) as caught:
        load_registry(session, None if via_env else private)
    assert str(caught.value) == "Реестр: файл проектов недоступен или содержит ошибку YAML (projects_file_unavailable)"


@pytest.mark.parametrize("contents", [
    "projects: [", "private-secret", "projects: [private-secret]", "projects: []", "projects: false", "projects: 0",
    "projects:\n  ONE: {results: one}\n  ONE: {results: secret}",
    "projects:\n  ONE:\n    results: one\n    stands:\n      duplicate: {}\n      duplicate: {title: secret}",
])
def test_invalid_or_duplicate_yaml_has_sanitized_global_error(session, tmp_path, contents):
    file = tmp_path / "secret-path.yaml"
    file.write_text(contents, encoding="utf-8")
    with pytest.raises(RegistryError) as caught:
        load_registry(session, file)
    assert "secret" not in str(caught.value)
    assert str(file) not in str(caught.value)


def test_yaml_read_permission_error_is_sanitized(session, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("private-path-secret")
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(RegistryError, match=r"\(projects_file_unavailable\)$"):
        load_registry(session, "explicit.yaml")


def test_yaml_anchors_and_explicit_overrides_keep_legacy_semantics(session, tmp_path):
    file = tmp_path / "projects.yaml"
    file.write_text("""defaults: &defaults
  results: default-results
  runner: newman
projects:
  ONE:
    <<: *defaults
    results: override-results
    runner: pytest
""", encoding="utf-8")
    project = load_registry(session, file).get("ONE")
    assert project.results == (tmp_path / "override-results").resolve()
    assert project.runner == "pytest"
    assert project == load_projects(file)[0]


@pytest.mark.parametrize("invalid", [None, ["secret"], {"results": ["secret"]},
                                      {"results": "results", "stands": ["secret"]},
                                      {"results": "results", "environments": "secret"}])
def test_malformed_yaml_project_does_not_hide_valid_neighbor(session, tmp_path, invalid):
    registry = load_registry(session, manifest(tmp_path, {"BAD": invalid, "GOOD": {"results": "results"}}))
    assert list(registry.projects) == ["GOOD"]
    assert "BAD" in registry.errors
    assert "secret" not in registry.errors["BAD"]
    assert {item["code"] for item in registry.summaries} == {"BAD", "GOOD"}
    with pytest.raises(RegistryError):
        registry.get("BAD")


def test_yaml_db_conflict_is_scoped_and_never_imports(session, tmp_path):
    session.add_all([Project(code="CONFLICT", title="DB", configuration={}), Project(code="OTHER", title="Other")])
    session.commit()
    registry = load_registry(session, manifest(tmp_path, {
        "CONFLICT": {"results": "yaml"}, "GOOD": {"results": "good"},
    }))
    assert set(registry.projects) == {"GOOD", "OTHER"}
    assert registry.errors == {"CONFLICT": "Проект CONFLICT: конфликт источников конфигурации (configuration_conflict)"}
    assert session.get(Project, "GOOD") is None
    assert session.get(Project, "CONFLICT").configuration == {}


@pytest.mark.parametrize("field,value", [("title", 42), ("description", ["secret"]),
                                         ("problem", ["secret"]), ("seed_info", ["secret"])])
def test_malformed_yaml_stand_public_text_is_scoped(session, tmp_path, field, value):
    registry = load_registry(session, manifest(tmp_path, {
        "BAD": {"results": "results", "stands": {"broken": {field: value}}},
        "GOOD": {"results": "results"},
    }))
    assert list(registry.projects) == ["GOOD"]
    assert "(invalid_stand)" in registry.errors["BAD"]
    assert "secret" not in registry.errors["BAD"]


@pytest.mark.parametrize("settings", [
    [], "secret", {"stands": {}}, {"commands": {"start": "secret"}}, {"secrets_files": ["secret"]},
    {"title": "secret"}, {"results": "relative/secret"}, {"tests": "relative/secret"},
    {"runner": None}, {"runner": ""}, {"tests_url": 12}, {"capability": []},
    {"environments": ["same", "same"]}, {"environments": [1]}, {"environments": "secret"},
])
def test_db_settings_use_strict_allowlist_and_types(session, tmp_path, settings):
    session.add_all([Project(code="BAD", title="Bad", configuration=settings),
                     Project(code="GOOD", title="Good", configuration={})])
    session.commit()
    registry = load_registry(session, manifest(tmp_path, {}))
    assert list(registry.projects) == ["GOOD"]
    assert "BAD" in registry.errors
    assert "secret" not in registry.errors["BAD"]


@pytest.mark.parametrize("collision", ["instance-id", "feature"])
def test_yaml_stand_collides_with_managed_id_or_human_name(session, tmp_path, collision):
    session.add(Project(code="ONE", title="One"))
    session.commit()
    add_stand(session, "ONE")
    registry = load_registry(session, manifest(tmp_path, {
        "ONE": {"results": "results", "stands": {collision: {}}}, "GOOD": {"results": "results"},
    }))
    assert registry.errors == {"ONE": "Проект ONE: конфликт имени или идентификатора стенда (duplicate_stand)"}
    assert list(registry.projects) == ["GOOD"]


@pytest.mark.parametrize("kind", ["yaml-yaml", "yaml-db", "db-db", "db-list"])
def test_duplicate_stand_environments_are_scoped(session, tmp_path, kind):
    session.add(Project(code="ONE", title="One"))
    session.commit()
    stands = {"legacy": {"environments": ["shared"]}} if kind.startswith("yaml") else {}
    if kind == "yaml-yaml":
        stands["legacy2"] = {"environments": ["shared"]}
    else:
        add_stand(session, "ONE", environments=["shared", "shared"] if kind == "db-list" else ["shared"])
    if kind == "db-db":
        add_stand(session, "ONE", id="second-id", name="second", environments=["shared"])
    registry = load_registry(session, manifest(tmp_path, {"ONE": {"results": "results", "stands": stands}}))
    assert registry.errors == {"ONE": "Проект ONE: окружение указано повторно или принадлежит нескольким стендам (duplicate_environments)"}


def test_managed_stands_expose_only_safe_metadata_and_explicit_state(session, tmp_path):
    session.add(Project(code="ONE", title="One", configuration={}))
    session.commit()
    add_stand(session, "ONE", status="created", environments=["managed"])
    session.add(StandBuild(id="build", project="ONE", stand_id="instance-id", template_key="http",
                           template_version="1", status="done", snapshot={"secret": "snapshot-secret"},
                           result={"secret": "result-secret"}, images=["image-secret"]))
    session.commit()
    stand = load_registry(session, manifest(tmp_path, {})).get("ONE").stands[0]
    assert (stand.name, stand.title, stand.source, stand.state) == ("instance-id", "feature", "database", "created")
    assert stand.environments == ["managed"]
    assert "Состояние:" in stand.description
    assert not any((stand.commands, stand.params, stand.access, stand.secrets_files, stand.info_file,
                    stand.test_data, stand.smoke, stand.workdir, stand.links, stand.access_roles))
    serialized = str(asdict(stand))
    assert "secret" not in serialized.replace("secrets_files", "")
    assert "template-env" not in serialized
    assert "ref-must" not in serialized


def test_fresh_sessions_see_db_changes_without_registry_cache(session, tmp_path):
    session.add(Project(code="ONE", title="First", configuration={}))
    session.commit()
    file = manifest(tmp_path, {})
    engine = session.get_bind()
    with Session(engine) as first:
        assert load_registry(first, file).get("ONE").title == "First"
    with Session(engine) as writer:
        writer.execute(update(Project).where(Project.code == "ONE").values(title="Second"))
        writer.commit()
    with Session(engine) as fresh:
        assert load_registry(fresh, file).get("ONE").title == "Second"


def test_registry_never_flushes_commits_or_writes(session, tmp_path):
    session.add(Project(code="ONE", title="One", configuration={}))
    session.commit()
    file = manifest(tmp_path, {"YAML": {"results": "results"}})
    session.add(Project(code="PENDING", title="Pending"))
    writes = []

    def sql(connection, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith("SELECT"):
            writes.append(statement)

    def forbidden(*args, **kwargs):
        pytest.fail("Registry attempted flush/commit")

    event.listen(session.get_bind(), "before_cursor_execute", sql)
    event.listen(session, "before_flush", forbidden)
    event.listen(session, "before_commit", forbidden)
    try:
        registry = load_registry(session, file)
        assert set(registry.projects) == {"ONE", "YAML"}
        assert len(session.new) == 1
        assert writes == []
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", sql)
        event.remove(session, "before_flush", forbidden)
        event.remove(session, "before_commit", forbidden)
    session.rollback()
    assert session.scalar(select(Project.code).where(Project.code == "PENDING")) is None
