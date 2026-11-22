from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from portal import auth, cli
from portal.models import Base, Project, User


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'bootstrap.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_cli_bootstrap_is_noninteractive_and_prints_credentials_only_on_creation(database, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(database))
    monkeypatch.setattr(cli, "ask_password", lambda: pytest.fail("bootstrap must not prompt"))
    monkeypatch.setattr("sys.argv", ["portal.cli", "user", "ensure-default-admin"])
    assert cli.main() == 0
    first = capsys.readouterr().out
    assert "Логин: admin" in first
    password = first.split("пароль: ", 1)[1].split(".\n", 1)[0]
    assert len(password) >= 40
    assert cli.main() == 0
    second = capsys.readouterr().out
    assert "не изменены" in second and "пароль:" not in second
    assert password not in second
    with Session(database) as session:
        user = session.scalars(select(User)).one()
        assert user.role == "admin" and user.active and not user.must_change_password
        assert user.password_change_suggested and auth.verify_password(password, user.password_hash)
        assert password not in user.password_hash
        assert auth.login(session, "admin", password)[0].login == "admin"


@pytest.mark.parametrize("login,role,active", [
    ("admin", "admin", True), ("custom-admin", "admin", True),
    ("only-viewer", "viewer", True), ("disabled", "viewer", False),
    ("admin", "admin", False),
])
def test_existing_users_are_never_changed_or_supplemented(database, monkeypatch, login, role, active):
    with Session(database, expire_on_commit=False) as session:
        user = auth.create_user(session, login, role, "existing-password", must_change=True)
        user.active = active
        user.failed_logins = 3
        user.locked_until = auth.now() + timedelta(minutes=10)
        user.password_change_suggested = True
        session.commit()
        original = (user.password_hash, user.locked_until)
        monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda size: pytest.fail("existing users need no password"))
        assert auth.ensure_default_admin(session) is None
        session.refresh(user)
        assert list(session.scalars(select(User.login))) == [login]
        assert (user.role, user.active, user.must_change_password, user.failed_logins) == (role, active, True, 3)
        assert (user.password_hash, user.locked_until) == original
        assert user.password_change_suggested is True


def test_two_simultaneous_bootstraps_create_exactly_one_admin(database):
    barrier = Barrier(2)

    def bootstrap():
        with Session(database) as session:
            barrier.wait(timeout=10)
            return auth.ensure_default_admin(session)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: bootstrap(), range(2)))
    assert results.count(None) == 1
    password = next(value for value in results if value is not None)
    with Session(database) as session:
        user = session.scalars(select(User)).one()
        assert user.login == "admin" and auth.verify_password(password, user.password_hash)


def test_empty_user_tables_get_unique_passwords_from_secure_generator(database, monkeypatch):
    secure_generator = auth.secrets.token_urlsafe
    sizes = []

    def generate(size):
        sizes.append(size)
        return secure_generator(size)

    monkeypatch.setattr(auth.secrets, "token_urlsafe", generate)
    passwords = []
    with Session(database) as session:
        for _ in range(3):
            password = auth.ensure_default_admin(session)
            assert password is not None and len(password) >= 40
            passwords.append(password)
            session.delete(session.get(User, "admin"))
            session.commit()
    assert len(set(passwords)) == len(passwords)
    assert sizes == [32, 32, 32]


def test_bootstrap_and_repeat_preserve_project_configuration(database):
    configuration = {"title": "Учебная установка", "environments": ["test"]}
    with Session(database) as session:
        session.add(Project(code="demo", title="Учебный проект", configuration=configuration))
        session.commit()
        assert auth.ensure_default_admin(session) is not None
        assert auth.ensure_default_admin(session) is None
        assert session.get(Project, "demo").configuration == configuration


@pytest.mark.parametrize("error_type", [OperationalError, IntegrityError])
def test_cli_failed_commit_does_not_disclose_password(database, monkeypatch, capsys, error_type):
    generated = "fictional-generated-password-for-error-test"
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda size: generated)

    class FailedCommitSession(Session):
        def commit(self):
            # Even a DB error whose diagnostics contain a secret must be hidden.
            raise error_type("INSERT", {"password": generated}, Exception(generated))

    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(database, class_=FailedCommitSession))
    monkeypatch.setattr("sys.argv", ["portal.cli", "user", "ensure-default-admin"])
    assert cli.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Не удалось" in output.err and generated not in output.err
    with Session(database) as session:
        assert session.scalar(select(User.login)) is None
