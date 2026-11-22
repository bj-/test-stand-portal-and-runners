from datetime import timedelta

import pytest
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portal import auth
from portal.db import get_session
from portal.main import app
from portal.models import ApiToken, Base, LoginSession, User

PASSWORD = "длинный-пароль-1"


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def signed_in(client: TestClient, session: Session, login: str, role: str) -> TestClient:
    auth.create_user(session, login, role, PASSWORD)
    assert client.post("/api/auth/login", json={"login": login, "password": PASSWORD}).status_code == 200
    return client


def test_personal_and_mutating_routes_require_login(client):
    # ТЗ гостевого доступа: только явно перечисленные предметные GET публичны.
    public_reads = {
        "/api/health", "/api/auth/me", "/api/projects", "/api/runs", "/api/runs/{run_id}",
        "/api/runs/{run_id}/report", "/api/runs/{run_id}/allure", "/api/runs/{base}/compare/{target}",
        "/api/bugs", "/api/bugs/{bug_id}", "/api/stats", "/api/help", "/api/help/pages/{slug}",
        "/api/projects/{code}/stands", "/api/projects/{code}/catalog", "/api/projects/{code}/catalog/tests",
        "/api/projects/{code}/catalog/branches", "/api/projects/{code}/catalog/test-ids",
        "/api/jobs", "/api/jobs/{job_id}", "/api/job-batches/{batch_id}",
    }
    checked = 0
    for path, methods in get_openapi(title="t", version="1", routes=app.routes)["paths"].items():
        for method in methods:
            if not (method == "get" and path in public_reads) and path != "/api/auth/login":
                url = path.replace("{", "").replace("}", "")
                response = client.request(method.upper(), url, json={})
                assert response.status_code == 401, f"{method} {path}"
                checked += 1
    assert checked >= 15
    assert client.get("/api/health").status_code == 200


def test_only_api_schema_is_public_to_viewer(client):
    # Адреса могут отвечать страницей приложения, но не документацией API.
    assert "swagger" not in client.get("/docs").text.lower()
    assert '"openapi"' not in client.get("/openapi.json").text
    assert "paths" in client.get("/api/openapi.json").json()


def test_login_cookie_is_httponly_and_session_is_hashed(client, session):
    auth.create_user(session, "ann", "viewer", PASSWORD)
    response = client.post("/api/auth/login", json={"login": "ann", "password": PASSWORD})
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    stored = session.scalars(select(LoginSession)).one()
    assert stored.token_hash != response.cookies[auth.COOKIE]
    assert client.get("/api/auth/me").json()["role"] == "viewer"
    assert session.get(User, "ann").password_hash.startswith("scrypt$")


def test_wrong_password_and_unknown_user_look_the_same(client, session):
    auth.create_user(session, "ann", "viewer", PASSWORD)
    wrong = client.post("/api/auth/login", json={"login": "ann", "password": "нет"})
    unknown = client.post("/api/auth/login", json={"login": "bob", "password": "нет"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_account_locks_after_failed_logins_and_then_recovers(client, session):
    auth.create_user(session, "ann", "viewer", PASSWORD)
    for _ in range(auth.MAX_FAILED_LOGINS):
        client.post("/api/auth/login", json={"login": "ann", "password": "нет"})
    assert client.post("/api/auth/login", json={"login": "ann", "password": PASSWORD}).status_code == 429
    session.get(User, "ann").locked_until = auth.now() - timedelta(seconds=1)
    assert client.post("/api/auth/login", json={"login": "ann", "password": PASSWORD}).status_code == 200


def test_expired_session_becomes_guest(client, session):
    signed_in(client, session, "ann", "viewer")
    session.scalars(select(LoginSession)).one().expires = auth.now() - timedelta(seconds=1)
    response = client.get("/api/auth/me")
    assert response.json()["is_guest"] is True
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/profile").status_code == 401


def test_logout_ends_session(client, session):
    signed_in(client, session, "ann", "viewer")
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").json()["is_guest"] is True
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/profile").status_code == 401


@pytest.mark.parametrize(("role", "users", "sync_allowed"), [
    ("admin", 200, True), ("tester", 403, True), ("developer", 403, False), ("viewer", 403, False),
])
def test_role_matrix_for_people(client, session, tmp_path, monkeypatch, role, users, sync_allowed):
    (tmp_path / "projects.yaml").write_text("projects: {}", encoding="utf-8")
    monkeypatch.setenv("PORTAL_PROJECTS", str(tmp_path / "projects.yaml"))
    signed_in(client, session, "ann", role)
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/users").status_code == users
    assert client.post("/api/sync").status_code == (200 if sync_allowed else 403)


def test_viewer_cannot_change_users(client, session):
    signed_in(client, session, "ann", "viewer")
    assert client.post("/api/users", json={"login": "x", "role": "admin"}).status_code == 403
    assert client.patch("/api/users/ann", json={"role": "admin"}).status_code == 403


def test_llm_token_reads_data_but_not_allure(client, session):
    headers = {"Authorization": f"Bearer {auth.create_token(session, 'session-1', 'llm')}"}
    assert client.get("/api/runs", headers=headers).status_code == 200
    assert client.get("/api/runs/x/report", headers=headers).status_code == 404  # отчёт открыт, прогона нет
    assert client.get("/api/runs/x/allure", headers=headers).status_code == 403
    assert client.get("/api/users", headers=headers).status_code == 403


def test_agent_token_cannot_read_data(client, session):
    headers = {"Authorization": f"Bearer {auth.create_token(session, 'agent-1', 'agent')}"}
    assert client.get("/api/runs", headers=headers).status_code == 403


def test_wrong_token_is_rejected(client):
    assert client.get("/api/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_new_user_must_change_password_before_anything_else(client, session):
    signed_in(client, session, "root", "admin")
    created = client.post("/api/users", json={"login": "ann", "role": "viewer"}).json()
    client.post("/api/auth/logout")

    temporary = created["password"]
    assert client.post("/api/auth/login", json={"login": "ann", "password": temporary}).json()["must_change_password"]
    assert client.get("/api/runs").status_code == 403
    assert client.post("/api/auth/password", json={"current": temporary, "new": "мало"}).status_code == 422
    assert client.post("/api/auth/password", json={"current": "нет", "new": PASSWORD}).status_code == 403
    assert client.post("/api/auth/password", json={"current": temporary, "new": PASSWORD}).status_code == 204
    assert client.get("/api/runs").status_code == 200


def test_reset_password_ends_user_sessions(client, session):
    signed_in(client, session, "root", "admin")
    auth.create_user(session, "ann", "viewer", PASSWORD)
    other = TestClient(app)
    other.post("/api/auth/login", json={"login": "ann", "password": PASSWORD})
    assert client.patch("/api/users/ann", json={"reset_password": True}).json()["password"]
    assert other.get("/api/auth/me").json()["is_guest"] is True
    assert other.get("/api/profile").status_code == 401


def test_admin_cannot_lock_himself_out(client, session):
    signed_in(client, session, "root", "admin")
    assert client.patch("/api/users/root", json={"active": False}).status_code == 409
    assert client.patch("/api/users/root", json={"role": "viewer"}).status_code == 409


def test_deactivated_user_loses_access(client, session):
    signed_in(client, session, "root", "admin")
    auth.create_user(session, "ann", "viewer", PASSWORD)
    other = TestClient(app)
    other.post("/api/auth/login", json={"login": "ann", "password": PASSWORD})
    client.patch("/api/users/ann", json={"active": False})
    assert other.get("/api/auth/me").json()["is_guest"] is True
    assert other.get("/api/profile").status_code == 401
    assert other.post("/api/auth/login", json={"login": "ann", "password": PASSWORD}).status_code == 401


def test_profile_edit_by_owner(client, session):
    signed_in(client, session, "ann", "tester")
    changed = client.patch("/api/profile", json={"name": "Анна", "email": "ann@example.com", "telegram": "ann_qa"}).json()
    assert (changed["name"], changed["email"], changed["telegram"]) == ("Анна", "ann@example.com", "@ann_qa")
    assert client.get("/api/profile").json()["last_login"].endswith("Z")
    # роль и активность у себя не меняются: лишние поля игнорируются
    client.patch("/api/profile", json={"role": "admin", "active": False})
    assert session.get(User, "ann").role == "tester" and session.get(User, "ann").active


@pytest.mark.parametrize("body", [{"email": "не-почта"}, {"telegram": "x"}, {"telegram": "имя"}, {"name": "я" * 201}])
def test_profile_rejects_bad_values(client, session, body):
    signed_in(client, session, "ann", "tester")
    assert client.patch("/api/profile", json=body).status_code == 422


def test_profile_empty_values_clear_fields(client, session):
    signed_in(client, session, "ann", "tester")
    client.patch("/api/profile", json={"email": "ann@example.com"})
    assert client.patch("/api/profile", json={"email": ""}).json()["email"] == ""


def test_admin_edits_user_card_and_unlocks(client, session):
    auth.create_user(session, "bob", "viewer", PASSWORD)
    for _ in range(auth.MAX_FAILED_LOGINS):
        client.post("/api/auth/login", json={"login": "bob", "password": "неверный-пароль"})
    signed_in(client, session, "boss", "admin")
    card = client.get("/api/users/bob").json()
    assert card["locked"] is True
    changed = client.patch("/api/users/bob", json={"name": "Боб", "email": "bob@example.com", "role": "tester", "unlock": True}).json()
    assert changed["locked"] is False and changed["role"] == "tester" and changed["email"] == "bob@example.com"
    assert client.get("/api/users/nobody").status_code == 404


def test_only_admin_reads_user_cards(client, session):
    auth.create_user(session, "bob", "viewer", PASSWORD)
    signed_in(client, session, "ann", "tester")
    assert client.get("/api/users/bob").status_code == 403
    assert client.patch("/api/users/bob", json={"email": "x@example.com"}).status_code == 403


def test_theme_in_profile_dark_by_default(client, session):
    signed_in(client, session, "ann", "tester")
    assert client.get("/api/auth/me").json()["theme"] == "dark"
    assert client.patch("/api/profile", json={"theme": "light"}).json()["theme"] == "light"
    assert client.get("/api/auth/me").json()["theme"] == "light"
    assert client.patch("/api/profile", json={"theme": "pink"}).status_code == 422


def test_guest_contract_creates_no_identity_records(client, session):
    assert client.get("/api/auth/me").json() == {
        "name": "Гость", "role": "viewer", "is_guest": True,
        "must_change_password": False, "password_change_suggested": False, "theme": "dark",
    }
    assert not list(session.scalars(select(User)))
    assert not list(session.scalars(select(LoginSession)))
    assert not list(session.scalars(select(ApiToken)))


def test_real_user_named_guest_is_not_guest(client, session):
    signed_in(client, session, "Гость", "viewer")
    assert client.get("/api/auth/me").json()["is_guest"] is False
    assert client.get("/api/profile").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/profile").status_code == 401


@pytest.mark.parametrize("authorization", ["Bearer wrong", "Bearer ", "Basic wrong"])
def test_bad_authorization_never_falls_back_to_guest(client, authorization):
    assert client.get("/api/auth/me", headers={"Authorization": authorization}).status_code == 401


def test_disabled_token_never_falls_back_to_guest(client, session):
    token = auth.create_token(session, "disabled", "viewer")
    session.get(ApiToken, auth.token_hash(token)).active = False
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("path,expected", [("/api/auth/me", 200), ("/api/profile", 401), ("/api/runs/missing", 404)])
def test_invalid_cookie_is_cleared_in_success_and_error_responses(client, path, expected):
    client.cookies.set(auth.COOKIE, "invalid-session")
    response = client.get(path)
    assert response.status_code == expected
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_default_admin_has_full_access_without_forced_change(client, session):
    password = auth.ensure_default_admin(session)
    assert password is not None
    result = client.post("/api/auth/login", json={"login": "admin", "password": password})
    assert result.status_code == 200
    assert result.json()["password_change_suggested"] is True
    assert result.json()["must_change_password"] is False
    assert result.json()["role"] == "admin"
    assert client.get("/api/users").status_code == 200
    assert client.get("/api/profile").status_code == 200
    assert client.get("/api/auth/me").json()["password_change_suggested"] is True


def test_keep_default_password_persists_across_login_and_bootstrap(client, session):
    password = auth.ensure_default_admin(session)
    client.post("/api/auth/login", json={"login": "admin", "password": password})
    before = session.get(User, "admin").password_hash
    assert client.post("/api/auth/password-suggestion/dismiss").status_code == 204
    assert client.post("/api/auth/password-suggestion/dismiss").status_code == 204
    assert auth.ensure_default_admin(session) is None
    assert session.get(User, "admin").password_hash == before
    client.post("/api/auth/logout")
    result = client.post("/api/auth/login", json={"login": "admin", "password": password})
    assert result.status_code == 200
    assert result.json()["password_change_suggested"] is False
    assert client.get("/api/auth/me").json()["password_change_suggested"] is False


def test_change_default_password_clears_suggestion_and_bootstrap_does_not_restore_it(client, session):
    password = auth.ensure_default_admin(session)
    client.post("/api/auth/login", json={"login": "admin", "password": password})
    assert client.post("/api/auth/password", json={"current": password, "new": "short"}).status_code == 422
    assert client.get("/api/auth/me").json()["password_change_suggested"] is True
    assert client.post("/api/auth/password", json={"current": password, "new": PASSWORD}).status_code == 204
    before = session.get(User, "admin").password_hash
    assert auth.ensure_default_admin(session) is None
    assert session.get(User, "admin").password_hash == before
    assert client.get("/api/auth/me").json()["password_change_suggested"] is False
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"login": "admin", "password": PASSWORD}).status_code == 200
    assert client.post("/api/auth/login", json={"login": "admin", "password": password}).status_code == 401


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_api_token_cannot_dismiss_suggestion_even_with_matching_user_name(client, session, role):
    password = auth.ensure_default_admin(session)
    token = auth.create_token(session, "admin", role)
    response = client.post("/api/auth/password-suggestion/dismiss", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert session.get(User, "admin").password_change_suggested is True
    # Even supplying the user's cookie must not turn a bearer request into a profile choice.
    client.post("/api/auth/login", json={"login": "admin", "password": password})
    assert client.post("/api/auth/password-suggestion/dismiss", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_suggestion_dismiss_cannot_bypass_forced_password_change(client, session):
    user = auth.create_user(session, "ann", "admin", PASSWORD, must_change=True)
    user.password_change_suggested = True
    session.commit()
    client.post("/api/auth/login", json={"login": "ann", "password": PASSWORD})
    assert client.post("/api/auth/password-suggestion/dismiss").status_code == 403
    assert user.must_change_password is True and user.password_change_suggested is True
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/auth/password", json={"current": PASSWORD, "new": PASSWORD + "-new"}).status_code == 204
    assert client.get("/api/auth/me").json()["password_change_suggested"] is False
    assert client.get("/api/users").status_code == 200


def test_reset_default_admin_ends_sessions_and_requires_change(client, session):
    password = auth.ensure_default_admin(session)
    signed_in(client, session, "boss", "admin")
    other = TestClient(app)
    other.post("/api/auth/login", json={"login": "admin", "password": password})
    reset = client.patch("/api/users/admin", json={"reset_password": True})
    assert reset.status_code == 200
    assert other.get("/api/auth/me").json()["is_guest"] is True
    changed = other.post("/api/auth/login", json={"login": "admin", "password": reset.json()["password"]}).json()
    assert changed["must_change_password"] is True and changed["password_change_suggested"] is False
    assert other.post("/api/auth/password-suggestion/dismiss").status_code == 403


@pytest.mark.parametrize("invalid", ["expired", "disabled", "deleted-session"])
def test_invalid_personal_session_cannot_dismiss_suggestion(client, session, invalid):
    password = auth.ensure_default_admin(session)
    client.post("/api/auth/login", json={"login": "admin", "password": password})
    record = session.scalars(select(LoginSession)).one()
    if invalid == "expired":
        record.expires = auth.now() - timedelta(seconds=1)
    elif invalid == "disabled":
        session.get(User, "admin").active = False
    else:
        session.delete(record)
    session.commit()
    assert client.post("/api/auth/password-suggestion/dismiss").status_code == 401
    assert session.get(User, "admin").password_change_suggested is True


def test_dismiss_changes_only_current_users_suggestion(client, session):
    auth.ensure_default_admin(session)
    user = auth.create_user(session, "other", "viewer", PASSWORD)
    user.password_change_suggested = True
    session.commit()
    client.post("/api/auth/login", json={"login": "other", "password": PASSWORD})
    assert client.post("/api/auth/password-suggestion/dismiss", json={"login": "admin"}).status_code == 204
    assert session.get(User, "other").password_change_suggested is False
    assert session.get(User, "admin").password_change_suggested is True
