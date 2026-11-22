"""Вход и роли (ADR 0002, п. 7). Пароли - scrypt, сессии и токены в БД хранятся только как хеш."""
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_session
from .models import ApiToken, LoginSession, User

COOKIE = "portal_session"
SESSION_TTL = timedelta(days=7)
MAX_FAILED_LOGINS = 5
LOCK_TIME = timedelta(minutes=15)
MIN_PASSWORD = 10

ADMIN, TESTER, DEVELOPER, VIEWER, AGENT, LLM = "admin", "tester", "developer", "viewer", "agent", "llm"
UPDATER = "updater"
USER_ROLES = (ADMIN, TESTER, DEVELOPER, VIEWER)
ALL_ROLES = (*USER_ROLES, AGENT, LLM, UPDATER)
READERS = (*USER_ROLES, LLM)


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    is_guest: bool = False


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(_scrypt(password, bytes.fromhex(salt)).hex(), digest)


# Проверяется, когда учётки нет: иначе её наличие видно по времени ответа.
_NO_USER_HASH = hash_password(secrets.token_hex(8))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"пароль короче {MIN_PASSWORD} символов")


def login(session: Session, name: str, password: str) -> tuple[User, str]:
    user = session.get(User, name)
    moment = now()
    if user is None or not user.active:
        verify_password(password, _NO_USER_HASH)
        raise HTTPException(401, "неверный логин или пароль")
    if user.locked_until and user.locked_until > moment:
        raise HTTPException(429, "учётка временно заблокирована, попробуйте позже")
    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = moment + LOCK_TIME
            user.failed_logins = 0
        session.commit()
        raise HTTPException(401, "неверный логин или пароль")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login = moment
    token = secrets.token_urlsafe(32)
    session.add(LoginSession(token_hash=token_hash(token), login=user.login, expires=moment + SESSION_TTL))
    session.commit()
    return user, token


def visitor(request: Request, session: Session = Depends(get_session)) -> Principal:
    header = request.headers.get("authorization", "")
    if header:
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "неверный токен")
        api_token = session.get(ApiToken, token_hash(header[7:].strip()))
        if api_token is None or not api_token.active:
            raise HTTPException(401, "неверный токен")
        if api_token.role == UPDATER and not (
            request.url.path == "/api/admin/updates" or request.url.path.startswith("/api/admin/updates/")
        ):
            raise HTTPException(403, "токен разрешает только управление обновлениями")
        return Principal(api_token.name, api_token.role)

    cookie = request.cookies.get(COOKIE)
    record = session.get(LoginSession, token_hash(cookie)) if cookie else None
    if record is None or record.expires <= now():
        if cookie:
            request.state.clear_portal_session = True
        return Principal("Гость", VIEWER, is_guest=True)
    user = session.get(User, record.login)
    if user is None or not user.active:
        request.state.clear_portal_session = True
        return Principal("Гость", VIEWER, is_guest=True)
    if user.must_change_password and not request.url.path.startswith("/api/auth/"):
        raise HTTPException(403, "password_change_required")
    return Principal(user.login, user.role)


def current_user(principal: Principal = Depends(visitor)) -> Principal:
    if principal.is_guest:
        raise HTTPException(401, "нужен вход")
    return principal


def cookie_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Личный выбор подтверждает браузерная сессия, а не имя токена API."""
    if request.headers.get("authorization"):
        raise HTTPException(403, "нужна сессия пользователя")
    cookie = request.cookies.get(COOKIE)
    record = session.get(LoginSession, token_hash(cookie)) if cookie else None
    user = session.get(User, record.login) if record and record.expires > now() else None
    if user is None or not user.active:
        raise HTTPException(401, "нужен вход")
    return user


def ensure_default_admin(session: Session) -> str | None:
    """Создать начального admin только в пустой БД, не меняя существующие учётки.

    Блокировка сериализует проверку пустоты и вставку, в том числе относительно
    обычного создания пользователя. Случайный пароль возвращается только после
    успешного commit; в БД сохраняется только хеш. Повтор возвращает None.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
    elif dialect == "sqlite":
        connection = session.connection().connection.driver_connection
        if not connection.in_transaction:
            session.execute(text("BEGIN IMMEDIATE"))
    else:
        raise RuntimeError(f"bootstrap не поддерживает БД {dialect}")
    if session.scalar(select(User.login).limit(1)) is not None:
        session.rollback()
        return None
    password = secrets.token_urlsafe(32)
    session.add(User(
        login="admin", name="", role=ADMIN, password_hash=hash_password(password),
        active=True, must_change_password=False, password_change_suggested=True,
        failed_logins=0,
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if session.scalar(select(User.login).limit(1)) is None:
            raise
        return None
    return password


def require_read(*roles: str):
    """Гостевой доступ подключается явно только к операциям просмотра."""
    def check(principal: Principal = Depends(visitor)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(403, "недостаточно прав")
        return principal

    return check


def require(*roles: str):
    def check(principal: Principal = Depends(current_user)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(403, "недостаточно прав")
        return principal

    return check


def create_user(session: Session, login_name: str, role: str, password: str, name: str = "", must_change: bool = False) -> User:
    if role not in USER_ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    user = User(
        login=login_name, name=name, role=role, password_hash=hash_password(password),
        active=True, must_change_password=must_change, failed_logins=0,
    )
    session.add(user)
    session.commit()
    return user


def create_token(session: Session, name: str, role: str) -> str:
    if role not in ALL_ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    token = secrets.token_urlsafe(32)
    session.add(ApiToken(token_hash=token_hash(token), name=name, role=role, active=True))
    session.commit()
    return token
