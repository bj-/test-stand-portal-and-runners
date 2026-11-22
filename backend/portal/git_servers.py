"""Installation Git server connections and write-only repository credentials."""

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import ADMIN, require
from .config import git_credentials_dir
from .db import get_session
from .git_credentials import Credential, CredentialError, read_credential, write_credential
from .git_provider import GitProviderError, list_repositories, normalize_server_url, repository_on_server
from .models import GitServer
from .test_data import DataRoute


router = APIRouter(prefix="/api/git-servers", route_class=DataRoute, dependencies=[Depends(require(ADMIN))])


class ServerTitle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("нужно печатное название Git-сервера")
        return value


class ServerIn(ServerTitle):
    base_url: str = Field(min_length=1, max_length=2048)
    provider: Literal["gitlab", "github", "other"]


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: SecretStr | None = Field(max_length=4096)
    username: str | None = Field(default=None, min_length=1, max_length=200)
    access: Literal["git", "api"]


def get_server(session: Session, server_id: str) -> GitServer:
    try:
        canonical = str(UUID(server_id))
    except ValueError:
        raise HTTPException(422, "неверный идентификатор Git-сервера") from None
    row = session.get(GitServer, canonical)
    if row is None:
        raise HTTPException(404, "Git-сервер не найден")
    return row


def server_url(server: GitServer, git_url: str | None) -> str:
    if git_url is None:
        raise HTTPException(422, "укажите URL репозитория на выбранном Git-сервере")
    try:
        return repository_on_server(server.base_url, git_url)
    except GitProviderError as error:
        raise HTTPException(422, str(error)) from None


def _default_username(server: GitServer) -> str:
    return "x-access-token" if server.provider == "github" else "oauth2"


def server_credential(server: GitServer) -> Credential:
    try:
        stored = read_credential(git_credentials_dir(), server.id)
    except CredentialError:
        raise HTTPException(503, "хранилище токенов Git недоступно") from None
    return stored if stored is not None else Credential(None, _default_username(server), "git")


def _public(server: GitServer, credential: Credential) -> dict:
    return {"id": server.id, "title": server.title, "base_url": server.base_url,
            "provider": server.provider, "has_token": credential.token is not None,
            "username": credential.username, "access": credential.access}


@router.get("")
def servers(session: Session = Depends(get_session)):
    return [_public(server, server_credential(server)) for server in session.scalars(
        select(GitServer).order_by(GitServer.title, GitServer.id))]


@router.post("", status_code=201)
def create_server(body: ServerIn, session: Session = Depends(get_session)):
    try:
        base_url = normalize_server_url(body.base_url)
    except GitProviderError as error:
        raise HTTPException(422, str(error)) from None
    server = GitServer(id=str(uuid4()), title=body.title, base_url=base_url, provider=body.provider)
    session.add(server)
    session.commit()
    return _public(server, Credential(None, _default_username(server), "git"))


@router.patch("/{server_id}")
def update_server(server_id: UUID, body: ServerTitle, session: Session = Depends(get_session)):
    server = get_server(session, str(server_id))
    credential = server_credential(server)
    server.title = body.title
    session.commit()
    return _public(server, credential)


@router.put("/{server_id}/token")
def store_token(server_id: UUID, body: CredentialIn, session: Session = Depends(get_session)):
    server = get_server(session, str(server_id))
    if body.access == "api" and server.provider == "other":
        raise HTTPException(422, "для этого Git-сервера репозиторий указывается вручную")
    value = body.token.get_secret_value() if body.token is not None else None
    if value is not None and (not value.isascii() or not value.isprintable() or any(char.isspace() for char in value) or not value):
        raise HTTPException(422, "введите непустой токен Git без пробелов")
    username = body.username if body.username is not None else server_credential(server).username
    if not username.isprintable() or any(char.isspace() for char in username) or ":" in username:
        raise HTTPException(422, "проверьте имя пользователя Git")
    credential = Credential(value, username, body.access)
    try:
        write_credential(git_credentials_dir(), server.id, credential)
    except CredentialError:
        raise HTTPException(503, "не удалось сохранить токен Git") from None
    return _public(server, credential)


@router.get("/{server_id}/repositories")
def repositories(server_id: UUID, page: int = Query(1, ge=1, le=1000), session: Session = Depends(get_session)):
    server = get_server(session, str(server_id))
    credential = server_credential(server)
    if credential.token is None or credential.access != "api" or server.provider == "other":
        raise HTTPException(409, "для списка нужен токен с доступом к API Git-сервера. Репозиторий можно указать вручную")
    try:
        return list_repositories(server.provider, server.base_url, credential.token, page)
    except GitProviderError as error:
        raise HTTPException(502, str(error)) from None
