import re
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import auth
from .auth import ADMIN, COOKIE, SESSION_TTL, USER_ROLES, Principal, current_user, now, require
from .config import cookie_secure
from .db import get_session
from .models import LoginSession, User

router = APIRouter(prefix="/api")

EMAIL = re.compile(r"^[^@\s]{1,100}@[^@\s]{1,100}\.[^@\s]{2,40}$")
TELEGRAM = re.compile(r"^@?[A-Za-z0-9_]{3,32}$")
THEMES = {"dark", "light", "system"}


class LoginIn(BaseModel):
    login: str
    password: str


class MeOut(BaseModel):
    name: str
    role: str
    is_guest: bool = False
    must_change_password: bool = False
    password_change_suggested: bool = False
    theme: str = "dark"


class PasswordIn(BaseModel):
    current: str
    new: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    login: str
    name: str
    role: str
    active: bool
    email: str = ""
    telegram: str = ""
    last_login: datetime | None = None
    theme: str = ""
    locked: bool = False  # вход заблокирован после неудачных попыток

    @field_validator("last_login")
    @classmethod
    def as_utc(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value else None


class UserIn(BaseModel):
    login: str
    name: str = ""
    role: str
    email: str = ""
    telegram: str = ""


class ProfilePatch(BaseModel):
    """Что пользователь меняет у себя сам. Роль и активность - только администратор."""
    name: str | None = None
    email: str | None = None
    telegram: str | None = None
    theme: str | None = None  # dark, light, system


class UserPatch(ProfilePatch):
    role: str | None = None
    active: bool | None = None
    reset_password: bool = False
    unlock: bool = False  # снять блокировку входа после неудачных попыток


class UserCreated(UserOut):
    password: str | None = None  # разовый пароль показывается один раз


def _temporary_password() -> str:
    return secrets.token_urlsafe(12)


def _drop_sessions(session: Session, login: str) -> None:
    session.execute(delete(LoginSession).where(LoginSession.login == login))


def _out(user: User, password: str | None = None) -> UserCreated:
    locked = bool(user.locked_until and user.locked_until > now())
    return UserCreated(**UserOut.model_validate(user).model_dump(exclude={"locked"}), locked=locked, password=password)


def _apply_profile(user: User, data: ProfilePatch) -> None:
    if data.name is not None:
        if len(data.name.strip()) > 200:
            raise HTTPException(422, "имя длиннее 200 символов")
        user.name = data.name.strip()
    if data.email is not None:
        email = data.email.strip()
        if email and not EMAIL.match(email):
            raise HTTPException(422, "недопустимый e-mail")
        user.email = email
    if data.telegram is not None:
        telegram = data.telegram.strip()
        if telegram and not TELEGRAM.match(telegram):
            raise HTTPException(422, "недопустимое имя в Telegram: латиница, цифры и _, от 3 до 32 символов")
        user.telegram = ("@" + telegram.lstrip("@")) if telegram else ""
    if data.theme is not None:
        if data.theme not in THEMES:
            raise HTTPException(422, "тема: dark, light или system")
        user.theme = data.theme


@router.post("/auth/login", response_model=MeOut)
def login(data: LoginIn, response: Response, session: Session = Depends(get_session)):
    user, token = auth.login(session, data.login, data.password)
    response.set_cookie(
        COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True, samesite="lax", secure=cookie_secure(),
    )
    return MeOut(name=user.login, role=user.role, must_change_password=user.must_change_password,
                 password_change_suggested=user.password_change_suggested, theme=user.theme or "dark")


@router.post("/auth/logout", status_code=204)
def logout(response: Response, principal: Principal = Depends(current_user), session: Session = Depends(get_session)):
    _drop_sessions(session, principal.name)
    session.commit()
    response.delete_cookie(COOKIE)


@router.get("/auth/me", response_model=MeOut)
def me(principal: Principal = Depends(auth.visitor), session: Session = Depends(get_session)):
    if principal.is_guest:
        return MeOut(name=principal.name, role=principal.role, is_guest=True)
    user = session.get(User, principal.name)
    return MeOut(name=principal.name, role=principal.role, must_change_password=bool(user and user.must_change_password),
                 password_change_suggested=bool(user and user.password_change_suggested),
                 theme=(user.theme if user else "") or "dark")


@router.post("/auth/password-suggestion/dismiss", status_code=204)
def dismiss_password_suggestion(user: User = Depends(auth.cookie_user), session: Session = Depends(get_session)):
    if user.must_change_password:
        raise HTTPException(403, "password_change_required")
    user.password_change_suggested = False
    session.commit()


@router.post("/auth/password", status_code=204)
def change_password(data: PasswordIn, principal: Principal = Depends(current_user), session: Session = Depends(get_session)):
    user = session.get(User, principal.name)
    if user is None:
        raise HTTPException(403, "у токена нет пароля")
    if not auth.verify_password(data.current, user.password_hash):
        raise HTTPException(403, "неверный текущий пароль")
    auth.check_new_password(data.new)
    user.password_hash = auth.hash_password(data.new)
    user.must_change_password = False
    user.password_change_suggested = False
    session.commit()


def _own_user(principal: Principal, session: Session) -> User:
    user = session.get(User, principal.name)
    if user is None:
        raise HTTPException(403, "у токена нет профиля")
    return user


@router.get("/profile", response_model=UserOut)
def profile(principal: Principal = Depends(require(*USER_ROLES)), session: Session = Depends(get_session)):
    return _out(_own_user(principal, session))


@router.patch("/profile", response_model=UserOut)
def update_profile(
    data: ProfilePatch, principal: Principal = Depends(require(*USER_ROLES)), session: Session = Depends(get_session),
):
    user = _own_user(principal, session)
    _apply_profile(user, data)
    session.commit()
    return _out(user)


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require(ADMIN))])
def users(session: Session = Depends(get_session)):
    return [_out(user) for user in session.scalars(select(User).order_by(User.login))]


@router.get("/users/{login}", response_model=UserOut, dependencies=[Depends(require(ADMIN))])
def user_detail(login: str, session: Session = Depends(get_session)):
    user = session.get(User, login)
    if user is None:
        raise HTTPException(404, "пользователь не найден")
    return _out(user)


@router.post("/users", response_model=UserCreated, status_code=201, dependencies=[Depends(require(ADMIN))])
def add_user(data: UserIn, session: Session = Depends(get_session)):
    if data.role not in USER_ROLES:
        raise HTTPException(422, "неизвестная роль")
    if not data.login.strip() or session.get(User, data.login):
        raise HTTPException(409, "логин пуст или занят")
    password = _temporary_password()
    user = auth.create_user(session, data.login, data.role, password, data.name, must_change=True)
    _apply_profile(user, ProfilePatch(email=data.email, telegram=data.telegram))
    session.commit()
    return _out(user, password)


@router.patch("/users/{login}", response_model=UserCreated)
def update_user(
    login: str,
    data: UserPatch,
    principal: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
):
    user = session.get(User, login)
    if user is None:
        raise HTTPException(404, "пользователь не найден")
    if login == principal.name and (data.active is False or (data.role and data.role != ADMIN)):
        raise HTTPException(409, "нельзя лишить прав самого себя: портал останется без администратора")
    if data.role is not None:
        if data.role not in USER_ROLES:
            raise HTTPException(422, "неизвестная роль")
        user.role = data.role
    _apply_profile(user, data)
    if data.active is not None:
        user.active = data.active
    password = None
    if data.reset_password:
        password = _temporary_password()
        user.password_hash = auth.hash_password(password)
        user.must_change_password = True
        user.password_change_suggested = False
    if data.reset_password or data.unlock:
        user.failed_logins = 0
        user.locked_until = None
    if data.reset_password or data.active is False:
        _drop_sessions(session, login)
    session.commit()
    return _out(user, password)
