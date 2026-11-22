"""API creation invariants: identity, immutable versions, local refs and atomic ports."""
import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portal import auth, git_servers
from portal.db import get_session
from portal.main import app
from portal.models import Base, Job, LoginSession, Project, StandBuild, StandInstance, StandPortReservation, User


def template():
    return {"schema_version": 1, "id": "http", "version": "1.0.0",
        "components": [{"id": "api", "repository": "source", "default_ref": "main", "dependencies": []}],
        "compose": {"component": "api", "path": "private-compose.yaml", "required_services": ["api"]},
        "profiles": {"laptop": {"services": ["api"], "resources": {"cpus": 1, "memory_mb": 512}}},
        "lifecycle": {stage: ([{"component": "api", "argv": ["private-command", stage]}]
                              if stage in ("build", "deploy", "healthcheck", "smoke") else [])
                      for stage in ("build", "deploy", "migrate", "seed", "healthcheck", "smoke")},
        "parameters": {"label": {"type": "string", "default": "demo"}}, "secret_refs": [],
        "smoke": {"runner": "api", "env": "local", "suite": "smoke.py"},
        "output_bindings": {"baseUrl": {"service": "api", "port": 8080, "scheme": "http", "path": "/"},
                            "alias": {"service": "api", "port": 8080, "scheme": "http", "path": "/other"}}}


def body(**extra):
    return {"name": "My stand", "template_key": "http", "template_version": "1.0.0", "target_host": "local",
            "profile": "laptop", "environments": ["feature"], **extra}


@pytest.fixture
def installation(tmp_path, monkeypatch):
    projects = tmp_path / "projects.yaml"
    projects.write_text("projects:\n  YAML:\n    results: results\n    environments: [alone]\n    stands:\n      legacy:\n        environments: [old]\n", encoding="utf-8")
    monkeypatch.setenv("PORTAL_PROJECTS", str(projects))
    repo = tmp_path / "private-source-path"
    repo.mkdir()
    def git(*args):
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()
    git("init", "--initial-branch=main")
    (repo / "README").write_text("fixture", encoding="utf-8")
    git("add", "README")
    git("-c", "user.name=Tests", "-c", "user.email=tests@example.invalid", "commit", "-m", "fixture")
    sha = git("rev-parse", "HEAD")
    hosts = tmp_path / "hosts.json"
    hosts.write_text(json.dumps({"schema_version": 1, "hosts": [{"id": "local", "title": "Local", "port_range": [24000, 24002],
        "repositories": {code: {"source": str(repo)} for code in ("ONE", "TWO", "YAML")}}]}), encoding="utf-8")
    monkeypatch.setenv("PORTAL_STAND_HOSTS", str(hosts))
    return repo, sha


@pytest.fixture
def session(installation):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([Project(code=code, title=code, configuration={"environments": ["standalone"]}) for code in ("ONE", "TWO")])
        for role in ("admin", "tester", "developer", "viewer"):
            db.add(User(login=role, name=role, role=role, password_hash="unused", active=True))
        db.flush()
        for role in ("admin", "tester", "developer", "viewer"):
            db.add(LoginSession(token_hash=auth.token_hash(role), login=role, expires=auth.now() + timedelta(days=1)))
        db.commit()
        yield db
    engine.dispose()


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        client.cookies.set(auth.COOKIE, "admin")
        yield client
    app.dependency_overrides.clear()


def register(client, code="ONE", spec=None):
    response = client.put(f"/api/projects/{code}/stand-repositories/source", json={"title": "Source", "components": ["api"]})
    assert response.status_code == 200, response.text
    response = client.post(f"/api/projects/{code}/stand-templates", json={"title": "HTTP", "spec": spec or template()})
    assert response.status_code in (200, 201), response.text


def create(client, payload=None, code="ONE", key="request"):
    return client.post(f"/api/projects/{code}/stand-instances", json=payload or body(), headers={"Idempotency-Key": key})


def counts(session):
    return tuple(session.scalar(select(func.count()).select_from(model)) for model in (StandInstance, Job, StandPortReservation, StandBuild))


def test_instance_name_check_tracks_names_ids_races_and_deleted_records(client, session):
    register(client)
    original = create(client).json()
    path = "/api/projects/ONE/stand-instance-name"
    before = counts(session)
    assert client.get(path, params={"name": "Fresh"}).json() == {"conflict": None}
    assert client.get(path, params={"name": "my stand"}).json() == {"conflict": None}
    expected = {"name": "My stand", "stand_id": original["id"], "suggested_name": "My stand - (admin)"}
    assert client.get(path, params={"name": " My stand "}).json() == {"conflict": expected}
    by_id = client.get(path, params={"name": original["id"]}).json()["conflict"]
    assert by_id["stand_id"] == original["id"] and by_id["name"] == original["id"]
    assert counts(session) == before
    assert client.get("/api/projects/TWO/stand-instance-name", params={"name": "My stand"}).json() == {"conflict": None}

    second = create(client, body(name=expected["suggested_name"], environments=["second"]), key="second")
    assert second.status_code == 201, second.text
    assert client.get(path, params={"name": "My stand"}).json()["conflict"]["suggested_name"] == "My stand - (admin) - 1"
    third = create(client, body(name="My stand - (admin) - 1", environments=["third"]), key="third")
    assert third.status_code == 201, third.text
    race = client.get(path, params={"name": "My stand - (admin)", "base_name": "My stand"}).json()["conflict"]
    assert race["suggested_name"] == "My stand - (admin) - 2"
    duplicate = create(client, body(name=original["id"]), key="duplicate")
    assert duplicate.status_code == 409 and isinstance(duplicate.json()["detail"], str)
    row = session.get(StandInstance, original["id"])
    row.deleted_at = auth.now()
    session.commit()
    assert client.get(path, params={"name": "My stand"}).json() == {"conflict": None}
    assert client.get(path, params={"name": original["id"]}).json() == {"conflict": None}


def test_instance_name_check_yaml_is_read_only_and_checks_public_title(client, session, tmp_path, monkeypatch):
    path = "/api/projects/YAML/stand-instance-name"
    assert client.get(path, params={"name": "legacy"}).json() == {"conflict": {
        "name": "legacy", "stand_id": "legacy", "suggested_name": "legacy - (admin)"}}
    assert session.get(Project, "YAML") is None
    manifest = tmp_path / "names.yaml"
    manifest.write_text("projects:\n  YAML:\n    results: results\n    stands:\n      configured:\n        title: Public stand\n", encoding="utf-8")
    monkeypatch.setenv("PORTAL_PROJECTS", str(manifest))
    result = client.get(path, params={"name": "Public stand"})
    assert result.status_code == 200 and result.json()["conflict"]["stand_id"] == "configured"
    assert counts(session) == (0, 0, 0, 0)


@pytest.mark.parametrize("role", ["admin", "tester", "developer"])
def test_instance_name_check_requires_creator_cookie(client, session, role):
    client.cookies.set(auth.COOKIE, role)
    path = "/api/projects/ONE/stand-instance-name"
    assert client.get(path, params={"name": "New"}).status_code == 200
    client.headers["Authorization"] = "Bearer " + auth.create_token(session, "token", role)
    assert client.get(path, params={"name": "New"}).status_code == 403
    del client.headers["Authorization"]
    client.cookies.set(auth.COOKIE, "viewer")
    assert client.get(path, params={"name": "New"}).status_code == 403
    client.cookies.clear()
    assert client.get(path, params={"name": "New"}).status_code in (401, 403)


@pytest.mark.parametrize("invalid", ["", " ", "line\nbreak", "x\x00x", "x" * 201])
def test_instance_name_check_reuses_creation_name_validation(client, session, invalid):
    path = "/api/projects/ONE/stand-instance-name"
    assert client.get(path, params={"name": invalid}).status_code == 422
    assert client.get(path, params={"name": "valid", "base_name": invalid}).status_code == 422
    assert counts(session) == (0, 0, 0, 0)


def test_instance_name_suggestion_preserves_suffix_and_unicode_length(client, session, tmp_path, monkeypatch):
    login = "old\nlogin\t\u200b"
    session.add(User(login=login, name="Old account", role="tester", password_hash="unused", active=True))
    session.flush()
    session.add(LoginSession(token_hash=auth.token_hash("old-account"), login=login, expires=auth.now() + timedelta(days=1)))
    name = "😀" * 200
    session.commit()
    manifest = tmp_path / "names.yaml"
    manifest.write_text(f"projects:\n  YAML:\n    results: results\n    stands:\n      configured:\n        title: {name}\n", encoding="utf-8")
    monkeypatch.setenv("PORTAL_PROJECTS", str(manifest))
    client.cookies.set(auth.COOKIE, "old-account")
    path = "/api/projects/YAML/stand-instance-name"
    result = client.get(path, params={"name": name})
    assert result.status_code == 200, result.text
    suggestion = result.json()["conflict"]["suggested_name"]
    assert len(suggestion) == 200 and suggestion.endswith(" - (old�login��)")
    from portal.managed_stands import InstanceNameIn
    assert InstanceNameIn(name=suggestion).name == suggestion
    assert client.get(path, params={"name": suggestion}).json() == {"conflict": None}
    assert counts(session) == (0, 0, 0, 0)


def test_preflight_observes_commit_without_writes_and_creation_freezes_template(client, session, installation):
    register(client)
    before = counts(session)
    response = client.post("/api/projects/ONE/stand-instances/preflight", json=body())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["components"] == [{"id": "api", "repository": "source", "requested_ref": "main", "observed_sha": installation[1]}]
    assert data["ports"] == {"baseUrl": 24000, "alias": 24000}
    assert data["executor_available"] is False
    assert counts(session) == before
    response = create(client)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["owner_login"] == "admin" and data["status"] == "waiting" and data["job_status"] == "queued"
    assert counts(session) == (1, 1, 1, 0)
    job = session.get(Job, data["job_id"])
    assert job.kind == "stand-build" and job.spec["template"] == template()
    assert job.spec["request"]["components"] == {"api": {"repository": "source", "ref": "main"}}
    assert job.spec["request"]["parameters"] == {"label": "demo"}
    assert "sha" not in job.spec["request"]["components"]["api"]
    assert str(installation[0]) not in json.dumps(job.spec)
    row = session.get(StandInstance, data["id"])
    assert len(row.request_hash) == 64 and row.resources == {"cpus": 1, "memory_mb": 512}
    assert client.get(f"/api/projects/ONE/stand-instances/{row.id}").json() == data


@pytest.mark.parametrize("role", ["viewer", "llm", "agent", "guest"])
def test_catalog_and_creation_writes_reject_unprivileged_roles(client, session, role):
    register(client)
    client.cookies.clear()
    if role == "viewer":
        client.cookies.set(auth.COOKIE, role)
    elif role != "guest":
        client.headers["Authorization"] = "Bearer " + auth.create_token(session, role, role)
    for method, path, payload in [("post", "/api/projects", {"code": "NEW", "title": "New", "configuration": {}}),
        ("post", "/api/projects/ONE/stand-repositories", {"git_url": "https://git.example/team/source.git", "components": ["api"]}),
        ("post", "/api/projects/ONE/stand-repositories/preview", {"git_url": "https://git.example/team/source.git", "git_server_id": "unavailable"}),
        ("put", "/api/projects/ONE/stand-repositories/source", {"title": "Source", "components": ["api"]}),
        ("post", "/api/projects/ONE/stand-templates", {"title": "HTTP", "spec": template()}),
        ("post", "/api/projects/ONE/stand-instances/preflight", body()),
        ("post", "/api/projects/ONE/stand-instances", body())]:
        response = getattr(client, method)(path, json=payload, headers={"Idempotency-Key": "denied"})
        assert response.status_code in (401, 403), response.text
    assert counts(session) == (0, 0, 0, 0)


def test_repository_create_derives_metadata_without_overwriting(client):
    catalog = "/api/projects/ONE/stand-repositories"
    payload = {"git_url": "https://Git.Example/team/subgroup/My-Repo.git", "components": ["api"]}
    response = client.post(catalog, json=payload)
    assert response.status_code == 201, response.text
    assert response.json() == {"key": "my-repo", "title": "My-Repo", "components": ["api"], "enabled": True,
                               "git_url": "https://git.example/team/subgroup/My-Repo.git", "git_server_id": None}
    assert client.post(catalog, json={**payload, "title": "Other"}).status_code == 409
    assert client.get(catalog).json() == [response.json()]
    updated = client.put(f"{catalog}/my-repo", json={"title": "Changed", "components": ["api"]}).json()
    assert updated["git_url"] == response.json()["git_url"]


def test_repository_preview_suggests_components_and_disambiguates_groups(client):
    git_server = client.post("/api/git-servers", json={"title": "GitLab", "base_url": "https://git.example", "provider": "gitlab"}).json()
    code = "/api/projects/ONE"
    catalog = f"{code}/stand-repositories"
    assert client.post(f"{code}/stand-templates", json={"title": "HTTP", "spec": template()}).status_code == 201

    source = {"git_url": "https://git.example/team/source.git", "git_server_id": git_server["id"]}
    preview = client.post(f"{catalog}/preview", json=source)
    assert preview.status_code == 200
    assert preview.json() == {"git_url": source["git_url"], "key": "source", "title": "source", "components": ["api"]}
    assert client.get(catalog).json() == []
    created = client.post(catalog, json=source)
    assert created.status_code == 201 and created.json()["components"] == ["api"]
    assert client.post(f"{catalog}/preview", json=source).status_code == 409

    second = {**source, "git_url": "https://git.example/other-team/source.git"}
    suggestion = client.post(f"{catalog}/preview", json=second).json()
    assert suggestion["key"] == "other-team-source" and suggestion["components"] == []
    created = client.post(catalog, json={**second, "components": ["worker"], "title": "Other service"})
    assert created.status_code == 201
    assert created.json()["key"] == suggestion["key"] and created.json()["components"] == ["worker"]
    assert created.json()["title"] == "Other service"

    third = {**source, "git_url": "https://git.example/other/team/source.git"}
    key = client.post(f"{catalog}/preview", json=third).json()["key"]
    assert key.startswith("other-team-source-") and len(key) <= 128
    assert client.post(catalog, json=third).json()["key"] == key
    assert client.post(f"{catalog}/preview", json={**source, "git_url": "https://other.example/team/source.git"}).status_code == 422


def test_repository_can_clear_component_suggestion(client):
    code = "/api/projects/ONE"
    assert client.post(f"{code}/stand-templates", json={"title": "HTTP", "spec": template()}).status_code == 201
    response = client.post(f"{code}/stand-repositories", json={"git_url": "https://git.example/team/source.git", "components": []})
    assert response.status_code == 201 and response.json()["components"] == []


@pytest.mark.parametrize("url", [
    "file:///tmp/source", "http://git.example/team/source", "https://user:private@git.example/team/source",
    "https://git.example/team/source?token=private", "https://git.example/team/../source",
])
def test_repository_api_rejects_non_https_or_embedded_credentials(client, url):
    response = client.post("/api/projects/ONE/stand-repositories", json={"git_url": url, "components": []})
    assert response.status_code == 422, response.text
    assert "private" not in response.text


def test_repository_catalog_cannot_create_server_side_copies(client):
    catalog = "/api/projects/ONE/stand-repositories"
    url = "https://git.example/team/source.git"
    assert client.post(catalog, json={"git_url": url, "components": ["api"]}).status_code == 201
    assert client.post(f"{catalog}/source/clone", json={}).status_code in (404, 405)


def test_git_server_keeps_token_write_only_and_reuses_rotated_token(client, monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_GIT_CREDENTIALS_DIR", str(tmp_path / "private"))
    server_api = "/api/git-servers"
    data = {"title": "GitLab", "base_url": "https://git.example/", "provider": "gitlab"}
    created = client.post(server_api, json=data)
    assert created.status_code == 201, created.text
    server = created.json()
    assert server["has_token"] is False and server["base_url"] == "https://git.example"
    url = f"{server_api}/{server['id']}"
    first = "synthetic-git-token-first"
    second = "synthetic-git-token-second"
    save = client.put(f"{url}/token", json={"token": first, "username": "fixture", "access": "git"})
    assert save.status_code == 200 and save.json()["has_token"] is True and first not in save.text
    assert client.get(f"{url}/repositories").status_code == 409
    catalog = "/api/projects/ONE/stand-repositories"
    payload = {"git_url": "https://git.example/team/source.git", "git_server_id": server["id"], "components": ["api"]}
    invalid = client.post(catalog, json={**payload, "git_url": "https://elsewhere.example/team/source.git"})
    assert invalid.status_code == 422
    response = client.post(catalog, json=payload)
    assert response.status_code == 201 and response.json()["git_server_id"] == server["id"]

    rotate = client.put(f"{url}/token", json={"token": second, "access": "api"})
    assert rotate.status_code == 200 and rotate.json()["username"] == "fixture" and second not in rotate.text

    listing = {"items": [{"name": "source", "path": "team/source", "git_url": payload["git_url"]}], "has_next": False, "page": 2}
    def discovery(provider, origin, token, page):
        assert (provider, origin, token, page) == ("gitlab", "https://git.example", second, 2)
        return listing
    monkeypatch.setattr(git_servers, "list_repositories", discovery)
    assert client.get(f"{url}/repositories?page=2").json() == listing
    assert second not in client.get(server_api).text
    deleted = client.put(f"{url}/token", json={"token": None, "access": "git"})
    assert deleted.status_code == 200 and deleted.json()["has_token"] is False
    assert client.get(f"{url}/repositories").status_code == 409


def test_git_server_mutations_and_discovery_require_admin(client):
    data = {"title": "GitLab", "base_url": "https://git.example", "provider": "gitlab"}
    server = client.post("/api/git-servers", json=data).json()
    url = f"/api/git-servers/{server['id']}"
    client.cookies.set(auth.COOKIE, "tester")
    assert client.get("/api/git-servers").status_code == 403
    assert client.get(f"{url}/repositories").status_code == 403
    assert client.patch(url, json={"title": "Changed"}).status_code == 403
    assert client.put(f"{url}/token", json={"token": "fixture", "access": "api"}).status_code == 403


def test_creation_requires_cookie_identity_and_password_guard(client, session):
    register(client)
    client.headers["Authorization"] = "Bearer " + auth.create_token(session, "admin-token", "admin")
    assert create(client).status_code == 403
    del client.headers["Authorization"]
    session.get(User, "admin").must_change_password = True
    session.commit()
    assert create(client).status_code == 403
    assert counts(session) == (0, 0, 0, 0)


def test_developer_can_create_only_owned_ephemeral_stand(client, session):
    register(client)
    client.cookies.set(auth.COOKIE, "developer")
    assert create(client).status_code == 403
    data = body(mode="ephemeral", expires=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    response = create(client, data)
    assert response.status_code == 201, response.text
    assert response.json()["owner_login"] == "developer"


def test_retry_precedes_live_catalog_and_expiry_validation(client, session, monkeypatch):
    register(client)
    payload = body(mode="ephemeral", expires=(datetime.now(UTC) + timedelta(seconds=60)).isoformat())
    first = create(client, payload)
    assert first.status_code == 201, first.text
    from portal import managed_stands
    def unavailable(*_):
        raise AssertionError("retry must not revalidate")
    monkeypatch.setattr(managed_stands, "_prepare", unavailable)
    assert create(client, payload).json() == first.json()
    assert create(client, payload).status_code == 200
    assert create(client, {**payload, "name": "Changed"}).status_code == 409
    client.cookies.set(auth.COOKIE, "tester")
    assert create(client, payload).status_code == 409
    assert counts(session) == (1, 1, 1, 0)


def test_expired_retry_with_disabled_repository_still_returns_original(client, session, monkeypatch):
    register(client)
    payload = body(mode="ephemeral", expires=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())
    first = create(client, payload)
    assert first.status_code == 201, first.text
    client.put("/api/projects/ONE/stand-repositories/source", json={"title": "Source", "components": ["api"], "enabled": False})
    from portal import managed_stands
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=1)
    monkeypatch.setattr(managed_stands, "datetime", Future)
    response = create(client, payload)
    assert response.status_code == 200 and response.json() == first.json()


def test_database_identity_conflict_rolls_back_all_creation_records(client, session, monkeypatch):
    register(client)
    register(client, "TWO")
    made = create(client).json()
    from portal import managed_stands
    class Collision:
        hex = made["id"].removeprefix("stand-")
    monkeypatch.setattr(managed_stands, "uuid4", Collision)
    response = create(client, code="TWO")
    assert response.status_code == 409, response.text
    assert counts(session) == (1, 1, 1, 0)


def test_pool_exhaustion_rolls_back_and_preserves_existing_allocations(client, session):
    register(client)
    for index in range(3):
        response = create(client, body(name=f"Stand {index}", environments=[f"env{index}"]), key=f"key{index}")
        assert response.status_code == 201, response.text
    assert create(client, body(name="Overflow", environments=["overflow"]), key="overflow").status_code == 409
    assert counts(session) == (3, 3, 3, 0)


@pytest.mark.parametrize("extra", [
    {"components": {"api": {"ref": "missing"}}}, {"components": {"api": {"ref": "main~1"}}},
    {"components": {"api": {"repository": "other"}}}, {"parameters": {"unknown": "value"}},
    {"profile": "unknown"}, {"target_host": "unknown"}, {"resources": {"cpus": 100}},
    {"mode": "ephemeral"}, {"mode": "ephemeral", "expires": "2020-01-01T00:00:00Z"},
    {"mode": "ephemeral", "expires": "2030-01-01T00:00:00"},
    {"expires": "2030-01-01T00:00:00Z"}, {"environments": ["dup", "dup"]},
    {"environments": ["../path"]}, {"ports": {"unknown": 24000}}, {"ports": {"baseUrl": True}},
    {"ports": {"baseUrl": 23999}}, {"ports": {"baseUrl": 24000, "alias": 24001}},
])
def test_invalid_creation_has_no_partial_records(client, session, extra):
    register(client)
    response = create(client, body(**extra))
    assert response.status_code == 422, response.text
    assert counts(session) == (0, 0, 0, 0)


@pytest.mark.parametrize("change", [{"enabled": False, "components": ["api"]}, {"enabled": True, "components": ["other"]}])
def test_disabled_or_incompatible_repository_is_rejected(client, session, change):
    register(client)
    assert client.put("/api/projects/ONE/stand-repositories/source", json={"title": "Source", **change}).status_code == 200
    assert create(client).status_code == 422
    assert counts(session) == (0, 0, 0, 0)


def test_template_versions_immutable_and_catalog_reads_hide_execution(client, session, installation):
    register(client)
    assert client.post("/api/projects/ONE/stand-templates", json={"title": "HTTP", "spec": template()}).status_code == 200
    changed = template()
    changed["lifecycle"]["build"][0]["argv"] = ["changed"]
    assert client.post("/api/projects/ONE/stand-templates", json={"title": "HTTP", "spec": changed}).status_code == 409
    for path in ("/api/stand-hosts", "/api/projects/ONE/stand-templates", "/api/projects/ONE/stand-repositories",
                 "/api/projects/ONE/stand-repositories/source/refs?host=local"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert str(installation[0]) not in response.text
        assert "private-command" not in response.text and "private-compose" not in response.text
    assert client.get("/api/projects/ONE/stand-repositories/source/refs?host=local").json() == [{"name": "main", "kind": "branch"}]
    assert client.get("/api/projects/ONE/stand-instances/absent").status_code == 404


def test_template_naming_is_version_scoped_and_preserves_portable_spec_and_refs(client, session):
    from portal.models import StandTemplate
    from portal.stand_contract import template_hash

    register(client)
    register(client, "TWO")
    newer = template()
    newer["version"] = "2.0.0"
    register(client, spec=newer)
    path = "/api/projects/ONE/stand-templates/http/1.0.0/naming"
    stored = session.get(StandTemplate, ("ONE", "http", "1.0.0"))
    original = copy.deepcopy(stored.spec)
    assert stored.name_ref_exclusions is None
    assert all(row["name_ref_exclusions"] == [] for row in client.get("/api/projects/ONE/stand-templates").json())
    response = client.patch(path, json={"name_ref_exclusions": ["api"]})
    assert response.status_code == 200, response.text
    expected = next(row for row in client.get("/api/projects/ONE/stand-templates").json() if row["version"] == "1.0.0")
    assert response.json() == expected and expected["name_ref_exclusions"] == ["api"]
    session.expire_all()
    assert session.get(StandTemplate, ("ONE", "http", "1.0.0")).name_ref_exclusions == ["api"]
    assert session.get(StandTemplate, ("ONE", "http", "2.0.0")).name_ref_exclusions is None
    assert session.get(StandTemplate, ("TWO", "http", "1.0.0")).name_ref_exclusions is None
    repeated = client.post("/api/projects/ONE/stand-templates", json={"title": "HTTP", "spec": original})
    assert repeated.status_code == 200 and repeated.json()["name_ref_exclusions"] == ["api"]
    changed = copy.deepcopy(original)
    changed["components"][0]["default_ref"] = "other"
    assert client.post("/api/projects/ONE/stand-templates", json={"title": "HTTP", "spec": changed}).status_code == 409
    preflight = client.post("/api/projects/ONE/stand-instances/preflight", json=body())
    assert preflight.status_code == 200 and preflight.json()["components"][0]["requested_ref"] == "main"
    made = create(client)
    assert made.status_code == 201, made.text
    job = session.get(Job, made.json()["job_id"])
    job_spec = copy.deepcopy(job.spec)
    assert job.spec["template"] == original
    assert job.spec["template_sha256"] == template_hash(original)
    assert job.spec["request"]["components"] == {"api": {"repository": "source", "ref": "main"}}
    assert session.get(StandInstance, made.json()["id"]).refs == job.spec["request"]["components"]
    assert client.patch(path, json={"name_ref_exclusions": []}).json()["name_ref_exclusions"] == []
    session.expire_all()
    assert session.get(Job, job.id).spec == job_spec
    assert session.get(StandTemplate, ("ONE", "http", "1.0.0")).spec == original


@pytest.mark.parametrize("payload", [
    {"name_ref_exclusions": ["missing"]}, {"name_ref_exclusions": ["api", "api"]},
    {"name_ref_exclusions": None}, {"name_ref_exclusions": "api"}, {"name_ref_exclusions": [1]},
    {"name_ref_exclusions": [], "spec": {}}, {},
])
def test_invalid_template_naming_keeps_previous_configuration(client, payload):
    register(client)
    path = "/api/projects/ONE/stand-templates/http/1.0.0/naming"
    assert client.patch(path, json={"name_ref_exclusions": ["api"]}).status_code == 200
    assert client.patch(path, json=payload).status_code == 422
    assert client.get("/api/projects/ONE/stand-templates").json()[0]["name_ref_exclusions"] == ["api"]


def test_template_naming_requires_admin_and_existing_version(client):
    register(client)
    path = "/api/projects/ONE/stand-templates/http/1.0.0/naming"
    payload = {"name_ref_exclusions": []}
    for role in ("tester", "developer", "viewer"):
        client.cookies.set(auth.COOKIE, role)
        assert client.patch(path, json=payload).status_code == 403
    client.cookies.set(auth.COOKIE, "admin")
    for missing in ("/api/projects/ONE/stand-templates/http/absent/naming",
                    "/api/projects/TWO/stand-templates/http/1.0.0/naming",
                    "/api/projects/ONE/stand-templates/absent/1.0.0/naming"):
        assert client.patch(missing, json=payload).status_code == 404


WIZARD_SETTINGS = "/api/projects/ONE/stand-templates/http/1.0.0/wizard-settings"


def test_wizard_settings_are_version_scoped_and_naming_preserves_technical_components(client, session):
    from portal.models import StandTemplate

    register(client)
    register(client, "TWO")
    newer = template()
    newer["version"] = "2.0.0"
    register(client, spec=newer)
    assert all(row["technical_components"] == [] for row in client.get("/api/projects/ONE/stand-templates").json())
    payload = {"name_ref_exclusions": ["api"], "technical_components": ["api"]}
    response = client.patch(WIZARD_SETTINGS, json=payload)
    assert response.status_code == 200, response.text
    assert all(response.json()[field] == values for field, values in payload.items())
    response = client.patch(WIZARD_SETTINGS.replace("wizard-settings", "naming"), json={"name_ref_exclusions": []})
    assert response.status_code == 200 and response.json()["technical_components"] == ["api"]
    session.expire_all()
    stored = session.get(StandTemplate, ("ONE", "http", "1.0.0"))
    assert stored.technical_components == ["api"] and stored.name_ref_exclusions == []
    assert stored.spec == template()
    for identity in (("ONE", "http", "2.0.0"), ("TWO", "http", "1.0.0")):
        assert session.get(StandTemplate, identity).technical_components is None
    repeated = client.post("/api/projects/ONE/stand-templates", json={"title": "HTTP", "spec": template()})
    assert repeated.status_code == 200 and repeated.json()["technical_components"] == ["api"]
    assert client.patch(WIZARD_SETTINGS, json={"name_ref_exclusions": [], "technical_components": []}).json()["technical_components"] == []


@pytest.mark.parametrize("field", ["name_ref_exclusions", "technical_components"])
@pytest.mark.parametrize("invalid", [["missing"], ["api", "api"], None, "api", [1]])
def test_wizard_settings_validate_both_lists_atomically(client, field, invalid):
    register(client)
    original = {"name_ref_exclusions": ["api"], "technical_components": ["api"]}
    assert client.patch(WIZARD_SETTINGS, json=original).status_code == 200
    payload = {"name_ref_exclusions": [], "technical_components": [], field: invalid}
    assert client.patch(WIZARD_SETTINGS, json=payload).status_code == 422
    stored = client.get("/api/projects/ONE/stand-templates").json()[0]
    assert all(stored[field] == value for field, value in original.items())


@pytest.mark.parametrize("payload", [{}, {"name_ref_exclusions": []}, {"technical_components": []},
                                      {"name_ref_exclusions": [], "technical_components": [], "spec": {}}])
def test_wizard_settings_require_both_lists_and_reject_extra_fields(client, payload):
    register(client)
    assert client.patch(WIZARD_SETTINGS, json=payload).status_code == 422


def test_wizard_settings_require_admin_and_existing_version(client):
    register(client)
    payload = {"name_ref_exclusions": [], "technical_components": []}
    for role in ("tester", "developer", "viewer"):
        client.cookies.set(auth.COOKIE, role)
        assert client.patch(WIZARD_SETTINGS, json=payload).status_code == 403
    client.cookies.clear()
    assert client.patch(WIZARD_SETTINGS, json=payload).status_code in (401, 403)
    client.cookies.set(auth.COOKIE, "admin")
    for missing in (WIZARD_SETTINGS.replace("1.0.0", "absent"), WIZARD_SETTINGS.replace("ONE", "TWO"),
                    WIZARD_SETTINGS.replace("http/", "absent/")):
        assert client.patch(missing, json=payload).status_code == 404


@pytest.mark.parametrize("choice", [{"repository": "alternative"}, {"ref": "HEAD"},
                                    {"repository": "alternative", "ref": "HEAD"}])
def test_technical_components_reject_overrides_in_preflight_and_create(client, session, choice):
    register(client)
    assert client.patch(WIZARD_SETTINGS, json={"name_ref_exclusions": [], "technical_components": ["api"]}).status_code == 200
    payload = body(components={"api": choice})
    before = counts(session)
    for response in (client.post("/api/projects/ONE/stand-instances/preflight", json=payload), create(client, payload)):
        assert response.status_code == 422 and "технического компонента api" in response.json()["detail"]
    assert counts(session) == before


@pytest.mark.parametrize("choices", [{}, {"api": {}}, {"api": {"repository": "source"}},
                                     {"api": {"ref": "main"}}, {"api": {"repository": "source", "ref": "main"}}])
def test_technical_components_accept_defaults_and_keep_portable_payload(client, session, choices):
    from portal.stand_contract import template_hash

    register(client)
    assert client.patch(WIZARD_SETTINGS, json={"name_ref_exclusions": [], "technical_components": ["api"]}).status_code == 200
    payload = body(components=choices)
    assert client.post("/api/projects/ONE/stand-instances/preflight", json=payload).status_code == 200
    made = create(client, payload)
    assert made.status_code == 201, made.text
    job = session.get(Job, made.json()["job_id"])
    original = copy.deepcopy(job.spec)
    assert job.spec["template"] == template() and job.spec["template_sha256"] == template_hash(template())
    assert job.spec["request"]["components"] == {"api": {"repository": "source", "ref": "main"}}
    assert client.patch(WIZARD_SETTINGS, json={"name_ref_exclusions": ["api"], "technical_components": []}).status_code == 200
    assert create(client, payload).status_code == 200
    session.expire_all()
    assert session.get(Job, job.id).spec == original


def test_regular_component_can_choose_alternative_repository_and_ref(client, session, monkeypatch, installation):
    spec = template()
    spec["components"].append({"id": "worker", "repository": "source", "default_ref": "main", "dependencies": ["api"]})
    register(client, spec=spec)
    for repository in ("source", "alternative"):
        assert client.put(f"/api/projects/ONE/stand-repositories/{repository}",
                          json={"title": repository, "components": ["api", "worker"]}).status_code == 200
    assert client.patch(WIZARD_SETTINGS, json={"name_ref_exclusions": [], "technical_components": ["api"]}).status_code == 200
    monkeypatch.setattr("portal.managed_stands.resolve_ref", lambda *args: installation[1])
    payload = body(components={"worker": {"repository": "alternative", "ref": "feature"}})
    response = client.post("/api/projects/ONE/stand-instances/preflight", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["components"][1]["repository"] == "alternative"
    made = create(client, payload)
    assert made.status_code == 201, made.text
    job = session.get(Job, made.json()["job_id"])
    original = copy.deepcopy(job.spec)
    assert job.spec["request"]["components"]["worker"] == {
        "repository": "alternative", "ref": "feature"}
    assert client.patch(WIZARD_SETTINGS, json={
        "name_ref_exclusions": [], "technical_components": ["api", "worker"]}).status_code == 200
    assert client.post("/api/projects/ONE/stand-instances/preflight", json=payload).status_code == 422
    retried = create(client, payload)
    assert retried.status_code == 200 and retried.json()["id"] == made.json()["id"]
    session.expire_all()
    assert session.get(Job, job.id).spec == original


def test_ports_conflict_across_projects_and_automatic_aliases(client, session):
    register(client)
    register(client, "TWO")
    first = create(client)
    assert first.status_code == 201
    response = create(client, body(ports={"alias": 24000}), code="TWO")
    assert response.status_code == 409
    assert counts(session) == (1, 1, 1, 0)
    response = create(client, code="TWO")
    assert response.status_code == 201, response.text
    assert response.json()["ports"] == {"baseUrl": 24001, "alias": 24001}
    assert counts(session) == (2, 2, 2, 0)


def test_different_service_cannot_reuse_port_and_rolls_back(client, session):
    spec = template()
    spec["compose"]["required_services"].append("other")
    spec["profiles"]["laptop"]["services"].append("other")
    spec["output_bindings"]["other"] = {"service": "other", "port": 8080, "scheme": "http", "path": "/"}
    register(client, spec=spec)
    response = create(client, body(ports={"baseUrl": 24000, "other": 24000}))
    assert response.status_code == 409, response.text
    assert counts(session) == (0, 0, 0, 0)


def test_yaml_catalog_gets_do_not_import_and_conflicts_are_rejected(client, session):
    for kind in ("stand-templates", "stand-repositories"):
        assert client.get(f"/api/projects/YAML/{kind}").status_code == 200
        assert session.get(Project, "YAML") is None
    register(client, "YAML")
    register(client)
    assert session.get(Project, "YAML").configuration is None
    for payload in (body(name="legacy"), body(environments=["alone"]), body(environments=["old"])):
        assert create(client, payload, code="YAML").status_code == 409
    assert create(client, body(environments=["standalone"])).status_code == 409
    assert counts(session) == (0, 0, 0, 0)


def test_existing_deployments_are_visible_without_becoming_creation_templates(client, session, monkeypatch, tmp_path):
    manifest = tmp_path / "deployment.yaml"
    manifest.write_text(
        """projects:
  YAML:
    results: results
    stands:
      dev:
        title: Development
        commands: {deploy: private-command}
        workdir: /private-path
        params:
          - {name: API_REF, title: API, default: feature/stand}
          - {name: DATABASE_PASSWORD, default: private-value}
      external:
        commands: {status: check}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTAL_PROJECTS", str(manifest))
    path = "/api/projects/YAML/stand-deployment-configurations"
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == [{
        "stand": "dev", "title": "Development",
        "refs": [{"name": "API_REF", "title": "API", "default": "feature/stand"}],
    }]
    assert client.get("/api/projects/YAML/stand-templates").json() == []
    assert session.get(Project, "YAML") is None
    client.cookies.set(auth.COOKIE, "tester")
    assert client.get(path).status_code == 403


def test_origin_catalog_lists_and_resolves_the_selected_branch(client, session, installation):
    repo, sha = installation
    for args in (
        ["update-ref", "refs/remotes/origin/feature/new", sha],
        ["update-ref", "refs/remotes/origin-tags/feature/new", sha],
        ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/feature/new"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    host_file = repo.parent / "hosts.json"
    config = json.loads(host_file.read_text(encoding="utf-8"))
    config["hosts"][0].update(ref_source="origin", publish_ip="10.20.30.40")
    host_file.write_text(json.dumps(config), encoding="utf-8")
    register(client)
    result = client.get("/api/projects/ONE/stand-repositories/source/refs?host=local")
    assert result.json() == [{"name": "feature/new", "kind": "branch"}, {"name": "feature/new", "kind": "tag"}]
    assert create(client, body(components={"api": {"ref": "feature/new"}})).status_code == 422
    response = create(client, body(components={"api": {"ref": "refs/heads/feature/new"}}))
    assert response.status_code == 201, response.text
    job = session.get(Job, response.json()["job_id"])
    assert job.spec["publish_ip"] == "10.20.30.40"
    assert job.spec["request"]["components"]["api"]["ref"] == "refs/heads/feature/new"


def test_endpoint_titles_are_available_before_build(client, session):
    from portal.models import StandTemplate

    spec = template()
    spec["output_bindings"]["baseUrl"]["title"] = "Веб-API"
    register(client, spec=spec)
    stored = session.get(StandTemplate, ("ONE", "http", "1.0.0"))
    stored.output_binding_titles = {"baseUrl": "Ignored override", "alias": "Интерфейс"}
    session.commit()
    expected = {"baseUrl": "Веб-API", "alias": "Интерфейс"}
    catalog = client.get("/api/projects/ONE/stand-templates")
    assert catalog.json()[0]["output_binding_titles"] == expected
    preflight = client.post("/api/projects/ONE/stand-instances/preflight", json=body())
    assert preflight.json()["output_binding_titles"] == expected
    assert create(client).json()["output_binding_titles"] == expected


def test_project_creation_is_explicit_validated_and_no_private_config_returned(client, session):
    for code in ("ONE", "YAML"):
        assert client.post("/api/projects", json={"code": code, "title": "Other", "configuration": {}}).status_code == 409
    assert client.post("/api/projects", json={"code": "NEW", "title": "New", "configuration": {"unknown": "private"}}).status_code == 422
    response = client.post("/api/projects", json={"code": "NEW", "title": "New", "configuration": {}})
    assert response.status_code == 201 and response.json() == {"code": "NEW", "title": "New", "source": "database"}
    assert session.get(Project, "NEW").configuration == {}


def test_secret_references_register_but_preflight_creation_wait_for_executor(client, session):
    spec = template()
    spec["secret_refs"] = ["database-password"]
    register(client, spec=spec)
    assert client.post("/api/projects/ONE/stand-instances/preflight", json=body()).status_code == 422
    assert create(client).status_code == 422
    assert "database-password" not in client.get("/api/projects/ONE/stand-templates").text
    assert counts(session) == (0, 0, 0, 0)


def test_idempotency_header_required_and_validation_never_echoes_payload(client):
    register(client)
    response = client.post("/api/projects/ONE/stand-instances", json=body())
    assert response.status_code == 422
    response = create(client, body(components={"api": {"command": "private-malformed-command"}}))
    assert response.status_code == 422 and "private-malformed-command" not in response.text


def test_invalid_unicode_and_path_are_sanitized_without_internal_errors(client, session):
    register(client)
    response = client.post("/api/projects", json={"code": "NEW", "title": "New", "configuration": {"results": "C:/private\u0000path"}})
    assert response.status_code == 422 and "private" not in response.text
    for extra in ({"template_key": "http\ud800"}, {"components": {"api": {"ref": "private\ud800"}}}):
        response = client.post("/api/projects/ONE/stand-instances", content=json.dumps(body(**extra)),
                               headers={"Idempotency-Key": "unicode", "Content-Type": "application/json"})
        assert response.status_code == 422 and "private" not in response.text
    assert counts(session) == (0, 0, 0, 0)


@pytest.mark.parametrize("path,payload", [
    ("/api/projects", {"code": "NEW", "title": "private\ud800", "configuration": {}}),
    ("/api/projects/ONE/stand-templates", {"title": "private\ud800", "spec": template()}),
    ("/api/projects/ONE/stand-templates", {"title": "HTTP", "spec": {**template(), "secret_refs": ["private\ud800"]}}),
])
def test_non_utf8_catalog_values_are_rejected_safely(client, path, payload):
    response = client.post(path, content=json.dumps(payload), headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "private" not in response.text


@pytest.mark.parametrize("extra", [
    {"title": "private\x00title"},
    {"configuration": {"tests_url": "private\x00url"}},
    {"configuration": {"private\x00key": "value"}},
])
def test_nul_metadata_is_rejected_before_database_write(client, session, extra):
    response = client.post("/api/projects", json={"code": "NEW", "title": "New", "configuration": {}, **extra})
    assert response.status_code == 422 and "private" not in response.text
    assert session.get(Project, "NEW") is None


def test_literal_backslash_unicode_text_is_not_a_nul(client):
    response = client.post("/api/projects", json={"code": "NEW", "title": r"literal\u0000", "configuration": {}})
    assert response.status_code == 201 and response.json()["title"] == r"literal\u0000"


def test_legacy_agent_cannot_claim_managed_job_and_cancel_preserves_ports(client, session):
    register(client)
    made = create(client).json()
    client.headers["Authorization"] = "Bearer " + auth.create_token(session, "worker", "agent")
    response = client.post("/api/agent/claim", json={"capabilities": ["stand-host", "stand-template-v1"]})
    assert response.status_code == 204, response.text
    del client.headers["Authorization"]
    response = client.post(f"/api/jobs/{made['job_id']}/cancel")
    assert response.status_code == 200, response.text
    updated = client.get(f"/api/projects/ONE/stand-instances/{made['id']}").json()
    assert updated["job_status"] == "cancelled" and updated["waiting_reason"] is None
    assert updated["status"] == "created" and updated["ports"] == made["ports"]
    assert counts(session) == (1, 1, 1, 0)
