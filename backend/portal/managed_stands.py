"""Каталог и атомарное создание стенда; исполнение portable v1 ещё не подключено."""
import copy
import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError,
                      field_serializer, field_validator, model_validator)
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import ADMIN, DEVELOPER, TESTER, USER_ROLES, Principal, cookie_user, now, require
from .db import get_session
from .git_servers import get_server, server_url
from .models import ApiToken, Job, Project, StandBuild, StandInstance, StandPortReservation, StandRepository, StandTemplate, User
from .registry import RegistryError, _db_project, _valid_code, load_registry
from .stand_catalog import CatalogError, list_refs, load_hosts, public_host, resolve_ref
from .stand_contract import ID, StandError, binding_titles, canonical, identifier, template_hash, validate_request, validate_template
from .stand_git import GitSourceError, parse_git_url
from .stands import _project, ensure_project, lock_key, locks
from .test_data import DataRoute
from .stand_test_data import (BundleIn, InitialDatasetIn, MAX_ENTRIES, applicable_bundle,
                              initialize_bundle, project_bundle, public_bundle, resolve_bundle)

router = APIRouter(prefix="/api", route_class=DataRoute)
readers = require(*USER_ROLES)
creators = require(ADMIN, TESTER, DEVELOPER)
admins = require(ADMIN)
data_managers = require(ADMIN, TESTER)
WAITING = "Ожидает подключения исполнителя шаблонов stand-template-v1"
MUTATION_NAMESPACE = 0x53545631
STAND_NAME_LIMIT = 200


def _end_of_day(expires: datetime) -> datetime:
    next_midnight = expires.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return next_midnight.astimezone(UTC).replace(tzinfo=None)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def valid_json(self):
        try:
            data = self.model_dump(mode="json")
            canonical(data)
            pending = [data]
            while pending:
                value = pending.pop()
                if isinstance(value, str) and "\x00" in value:
                    raise ValueError("NUL")
                if isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("нужен корректный JSON без NUL, NaN, Infinity и неполных Unicode символов") from None
        return self


class ProjectIn(Input):
    code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    configuration: dict
    initial_test_data: list[InitialDatasetIn] = Field(default_factory=list, max_length=MAX_ENTRIES)


class RepositoryFields(Input):
    components: list[str]
    enabled: StrictBool = True
    git_url: str | None = Field(default=None, max_length=2048)
    git_server_id: str | None = Field(default=None, max_length=36)


class RepositoryIn(RepositoryFields):
    title: str = Field(min_length=1, max_length=200)


class RepositoryCreateIn(RepositoryFields):
    components: list[str] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    key: str | None = Field(default=None, min_length=1, max_length=128)


class RepositoryPreviewIn(Input):
    git_url: str = Field(min_length=1, max_length=2048)
    git_server_id: str = Field(min_length=1, max_length=36)


class TemplateIn(Input):
    title: str = Field(min_length=1, max_length=200)
    spec: dict


class TemplateNamingIn(Input):
    name_ref_exclusions: list[str]


class TemplateWizardSettingsIn(TemplateNamingIn):
    technical_components: list[str]


class ComponentIn(Input):
    repository: str | None = None
    ref: str | None = None


class InstanceNameIn(Input):
    name: str = Field(min_length=1, max_length=STAND_NAME_LIMIT)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("нужно непустое печатное имя")
        return value


class InstanceIn(InstanceNameIn):
    install_test_data: StrictBool = True
    template_key: str
    template_version: str
    target_host: str
    profile: str
    components: dict[str, ComponentIn] = Field(default_factory=dict)
    parameters: dict = Field(default_factory=dict)
    environments: list[str] = Field(default_factory=list)
    mode: Literal["permanent", "ephemeral"] = "permanent"
    expires: datetime | None = None
    ports: dict[str, StrictInt] = Field(default_factory=dict)

    @field_validator("expires", mode="before")
    @classmethod
    def expiration(cls, value):
        # JSON datetime remains explicit even with strict validation of other fields.
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("нужна дата с часовым поясом") from None
        if value is not None:
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("нужна дата с часовым поясом")
            try:
                value.astimezone(UTC)
                _end_of_day(value)
            except OverflowError:
                raise ValueError("срок окончания вне допустимого диапазона") from None
        return value

    @field_serializer("expires", when_used="json")
    def serialize_expiration(self, value: datetime | None) -> datetime | None:
        # Preserve the UTC request representation used by existing idempotency hashes.
        return value.astimezone(UTC) if value is not None else None

    @field_validator("parameters")
    @classmethod
    def json_parameters(cls, value):
        try:
            canonical(value)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("неверные JSON параметры") from None
        return value


def _id(value: str) -> str:
    try:
        identifier(value, "identifier")
    except StandError:
        raise HTTPException(422, "неверный идентификатор") from None
    return value


def _mutation_lock(session: Session) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:namespace, 1)"), {"namespace": MUTATION_NAMESPACE})
    elif dialect == "sqlite":
        connection = session.connection().connection.driver_connection
        if not connection.in_transaction:
            session.execute(text("BEGIN IMMEDIATE"))
    else:
        raise HTTPException(503, "создание стендов недоступно для этой БД")


def _commit(session: Session):
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "конфликт каталога, имени, запроса или порта") from None


def _host(key):
    try:
        host = load_hosts().get(key)
    except CatalogError:
        raise HTTPException(422, "каталог хостов недоступен или некорректен") from None
    if host is None:
        raise HTTPException(422, "неизвестный целевой хост")
    return host


def _repository_out(row):
    return {"key": row.key, "title": row.title, "components": row.components, "enabled": row.enabled,
            "git_url": row.git_url, "git_server_id": row.git_server_id}


def _repository_url(url):
    if url is None:
        return None, None, None
    try:
        return parse_git_url(url)
    except GitSourceError as error:
        raise HTTPException(422, str(error)) from None


def _repository_source(session, git_url, server_id):
    if server_id is None:
        return git_url, None
    server = get_server(session, server_id)
    return server_url(server, git_url), server.id


def _new_repository_key(session: Session, code: str, git_url: str, server_id: str | None, base_key: str) -> str:
    existing = session.execute(select(StandRepository.key, StandRepository.git_url, StandRepository.git_server_id)
                               .where(StandRepository.project == code)).all()
    if any(url == git_url and connection == server_id for _, url, connection in existing):
        raise HTTPException(409, "репозиторий уже зарегистрирован для этого подключения")
    keys = {key for key, _, _ in existing}
    if base_key not in keys:
        return base_key

    group = urlsplit(git_url).path.lstrip("/").rpartition("/")[0].lower()
    group = re.sub(r"[^a-z0-9_.-]+", "-", group).strip("-._")
    qualified = f"{group}-{base_key}" if group else base_key
    if not qualified[0].isalpha():
        qualified = "repo-" + qualified
    if len(qualified) <= 128 and qualified not in keys:
        return qualified

    digest = hashlib.sha256(git_url.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{digest}"
    key = qualified[:128 - len(suffix)] + suffix
    index = 2
    while key in keys:
        suffix = f"-{digest}-{index}"
        key = qualified[:128 - len(suffix)] + suffix
        index += 1
    return key


def _repository_components(session: Session, code: str, key: str) -> list[str]:
    components = set()
    for spec in session.scalars(select(StandTemplate.spec).where(StandTemplate.project == code)):
        declared = spec.get("components") if isinstance(spec, dict) else None
        if not isinstance(declared, list):
            continue
        for component in declared:
            if isinstance(component, dict) and component.get("repository") == key:
                name = component.get("id")
                if isinstance(name, str) and ID.fullmatch(name):
                    components.add(name)
    return sorted(components)


def _repository_fields(key, title, components):
    _id(key)
    for component in components:
        _id(component)
    if len(set(components)) != len(components):
        raise HTTPException(422, "повтор компонента")
    if not title.strip() or len(title) > 200:
        raise HTTPException(422, "название репозитория должно содержать от 1 до 200 символов")


def _template_out(row):
    spec = row.spec
    # Explicit projection: commands, compose paths and secret references are private.
    return {"id": row.key, "version": row.version, "title": row.title,
            "name_ref_exclusions": row.name_ref_exclusions or [],
            "technical_components": row.technical_components or [],
            "output_binding_titles": binding_titles(spec, row.output_binding_titles),
            "components": [{key: component[key] for key in ("id", "repository", "default_ref", "dependencies")
                            if key in component} for component in spec.get("components", [])],
            "profiles": {key: {field: value[field] for field in ("services", "resources") if field in value}
                         for key, value in spec.get("profiles", {}).items()},
            "data_modules": {module: {"title": value.get("title", module), "service": value["service"],
                                       "format": value["format"]}
                             for module, value in spec.get("data_modules", {}).items()},
            "parameters": {key: {field: value[field] for field in ("type", "default", "required", "enum", "title", "description")
                                 if field in value} for key, value in spec.get("parameters", {}).items()}}


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, principal: Principal = Depends(admins), session: Session = Depends(get_session)):
    if not _valid_code(body.code) or not body.title.strip():
        raise HTTPException(422, "неверный код или название проекта")
    row = Project(code=body.code, title=body.title, configuration=body.configuration)
    try:
        canonical(body.configuration)
        if any(isinstance(body.configuration.get(key), str) and "\x00" in body.configuration[key]
               for key in ("results", "tests")):
            raise ValueError("invalid path")
        _db_project(row)
    except (RegistryError, OSError, ValueError, TypeError, UnicodeError):
        raise HTTPException(422, "неверная конфигурация проекта") from None
    _mutation_lock(session)
    try:
        registry = load_registry(session)
        if any(item["code"] == body.code for item in registry.summaries) or session.get(Project, body.code):
            raise HTTPException(409, "проект уже существует")
        session.add(row)
        session.flush()
        initialize_bundle(session, row, body.initial_test_data, principal)
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return {"code": row.code, "title": row.title, "source": "database"}


@router.get("/projects/{code}/stand-data-bundle")
def get_data_bundle(code: str, _=Depends(creators), session: Session = Depends(get_session)):
    _project(session, code)
    return project_bundle(session, code)


@router.put("/projects/{code}/stand-data-bundle")
def put_data_bundle(code: str, body: BundleIn, _=Depends(data_managers), session: Session = Depends(get_session)):
    _mutation_lock(session)
    try:
        project = _project(session, code)
        bundle = resolve_bundle(session, code, body)
        ensure_project(session, project)
        session.get(Project, code).stand_data_bundle = bundle
        _commit(session)
        return bundle
    except Exception:
        session.rollback()
        raise


@router.get("/stand-hosts")
def hosts(_=Depends(readers)):
    try:
        return [public_host(host) for host in load_hosts().values()]
    except CatalogError:
        raise HTTPException(422, "каталог хостов недоступен или некорректен") from None


@router.get("/projects/{code}/stand-repositories")
def repositories(code: str, _=Depends(readers), session: Session = Depends(get_session)):
    _project(session, code)
    return [_repository_out(row) for row in session.scalars(select(StandRepository).where(
        StandRepository.project == code).order_by(StandRepository.key))]


@router.post("/projects/{code}/stand-repositories/preview")
def preview_repository(code: str, body: RepositoryPreviewIn, _=Depends(admins), session: Session = Depends(get_session)):
    _project(session, code)
    git_url, title, base_key = _repository_url(body.git_url)
    git_url, server_id = _repository_source(session, git_url, body.git_server_id)
    key = _new_repository_key(session, code, git_url, server_id, base_key)
    return {"git_url": git_url, "key": key, "title": title[:200],
            "components": _repository_components(session, code, key)}


@router.post("/projects/{code}/stand-repositories", status_code=201)
def create_repository(code: str, body: RepositoryCreateIn, _=Depends(admins), session: Session = Depends(get_session)):
    git_url, default_title, default_key = _repository_url(body.git_url)
    title = body.title or (default_title[:200] if default_title else None)
    if not (body.key or default_key) or not title:
        raise HTTPException(422, "укажите Git URL или ключ и название репозитория")
    _mutation_lock(session)
    project = _project(session, code)
    git_url, server_id = _repository_source(session, git_url, body.git_server_id)
    if body.key:
        key = body.key
        if session.get(StandRepository, (code, key)) is not None:
            raise HTTPException(409, "репозиторий с таким ключом уже зарегистрирован")
    else:
        key = _new_repository_key(session, code, git_url, server_id, default_key)
    components = body.components if body.components is not None else _repository_components(session, code, key)
    _repository_fields(key, title, components)
    ensure_project(session, project)
    row = StandRepository(project=code, key=key, title=title, components=components,
                          enabled=body.enabled, git_url=git_url, git_server_id=server_id)
    session.add(row)
    _commit(session)
    return _repository_out(row)


@router.put("/projects/{code}/stand-repositories/{key}")
def put_repository(code: str, key: str, body: RepositoryIn, _=Depends(admins), session: Session = Depends(get_session)):
    _repository_fields(key, body.title, body.components)
    git_url, _, _ = _repository_url(body.git_url)
    _mutation_lock(session)
    try:
        project = _project(session, code)
        ensure_project(session, project)
        row = session.get(StandRepository, (code, key))
        if row is None:
            row = StandRepository(project=code, key=key)
            session.add(row)
        row.title, row.components, row.enabled = body.title, body.components, body.enabled
        url = git_url if "git_url" in body.model_fields_set else row.git_url
        server_id = body.git_server_id if "git_server_id" in body.model_fields_set else row.git_server_id
        row.git_url, row.git_server_id = _repository_source(session, url, server_id)
        _commit(session)
        return _repository_out(row)
    except Exception:
        session.rollback()
        raise


@router.get("/projects/{code}/stand-repositories/{key}/refs")
def refs(code: str, key: str, host: str, _=Depends(readers), session: Session = Depends(get_session)):
    _project(session, code)
    row = session.get(StandRepository, (code, key))
    if row is None or not row.enabled:
        raise HTTPException(404, "репозиторий не найден или отключён")
    target = _host(host)
    try:
        return list_refs(target, code, key)
    except CatalogError:
        raise HTTPException(422, "локальные refs репозитория недоступны") from None


@router.get("/projects/{code}/stand-templates")
def templates(code: str, _=Depends(readers), session: Session = Depends(get_session)):
    _project(session, code)
    return [_template_out(row) for row in session.scalars(select(StandTemplate).where(
        StandTemplate.project == code).order_by(StandTemplate.key, StandTemplate.version))]


@router.get("/projects/{code}/stand-deployment-configurations")
def deployment_configurations(code: str, _=Depends(admins), session: Session = Depends(get_session)):
    """Show the deployment presets used by the installation, without exposing its commands or access settings."""
    project = _project(session, code)
    return [
        {
            "stand": stand.name,
            "title": stand.title,
            "refs": [{"name": param.name, "title": param.title, "default": param.default}
                     for param in stand.params if param.name.endswith("_REF")],
        }
        for stand in project.stands if stand.source == "yaml" and "deploy" in stand.commands
    ]


@router.post("/projects/{code}/stand-templates", status_code=201)
def put_template(code: str, body: TemplateIn, response: Response, _=Depends(admins), session: Session = Depends(get_session)):
    try:
        validate_template(body.spec)
    except (StandError, ValueError, TypeError, RecursionError):
        raise HTTPException(422, "неверный portable v1 шаблон") from None
    if not body.title.strip():
        raise HTTPException(422, "пустое название шаблона")
    _mutation_lock(session)
    try:
        project = _project(session, code)
        existing = session.get(StandTemplate, (code, body.spec["id"], body.spec["version"]))
        if existing is not None:
            if existing.title != body.title or canonical(existing.spec) != canonical(body.spec):
                raise HTTPException(409, "версия шаблона неизменяема; зарегистрируйте новую версию")
            response.status_code = 200
            session.rollback()
            return _template_out(existing)
        ensure_project(session, project)
        row = StandTemplate(project=code, key=body.spec["id"], version=body.spec["version"],
                            title=body.title, spec=copy.deepcopy(body.spec))
        session.add(row)
        _commit(session)
        return _template_out(row)
    except Exception:
        session.rollback()
        raise


@router.patch("/projects/{code}/stand-templates/{key}/{version}/naming")
def patch_template_naming(code: str, key: str, version: str, body: TemplateNamingIn,
                          _=Depends(admins), session: Session = Depends(get_session)):
    return _patch_template_settings(session, code, key, version, body.model_dump())


@router.patch("/projects/{code}/stand-templates/{key}/{version}/wizard-settings")
def patch_template_wizard_settings(code: str, key: str, version: str, body: TemplateWizardSettingsIn,
                                   _=Depends(admins), session: Session = Depends(get_session)):
    return _patch_template_settings(session, code, key, version, body.model_dump())


def _patch_template_settings(session: Session, code: str, key: str, version: str, settings: dict[str, list[str]]):
    _mutation_lock(session)
    try:
        _project(session, code)
        row = session.get(StandTemplate, (code, key, version))
        if row is None:
            raise HTTPException(404, "версия шаблона не найдена")
        components = {component["id"] for component in row.spec.get("components", [])}
        for values in settings.values():
            if len(set(values)) != len(values):
                raise HTTPException(422, "повтор компонента")
            if set(values) - components:
                raise HTTPException(422, "неизвестный компонент шаблона")
        for field, values in settings.items():
            setattr(row, field, list(values))
        _commit(session)
        return _template_out(row)
    except Exception:
        session.rollback()
        raise


def _ports(session, host, template, requested):
    bindings = template["output_bindings"]
    if requested.keys() - bindings.keys():
        raise HTTPException(422, "неизвестная привязка порта")
    groups = {}
    for name, binding in bindings.items():
        groups.setdefault((binding["service"], binding["port"]), []).append(name)
    used = set(session.scalars(select(StandPortReservation.port).where(
        StandPortReservation.target_host == host.id, StandPortReservation.protocol == "tcp")))
    allocations, mapping = set(), {}
    # Allocate explicit requests before automatic ones so automatic selection cannot steal them.
    ordered = sorted(groups.values(), key=lambda names: not any(name in requested for name in names))
    for names in ordered:
        choices = {requested[name] for name in names if name in requested}
        if len(choices) > 1:
            raise HTTPException(422, "алиасы одного сервиса должны использовать один порт")
        port = next(iter(choices)) if choices else next((value for value in range(host.port_min, host.port_max + 1)
                                                       if value not in used and value not in allocations), None)
        if port is None:
            raise HTTPException(409, "свободных портов нет")
        if not host.port_min <= port <= host.port_max:
            raise HTTPException(422, "порт вне разрешённого диапазона хоста")
        if port in used or port in allocations:
            raise HTTPException(409, "порт уже зарезервирован")
        allocations.add(port)
        mapping.update({name: port for name in names})
    return mapping, sorted(allocations)


def _name_conflict(project, name):
    return next((stand for stand in project.stands if name in (stand.name, stand.title)), None)


def _suggest_name(project, base_name: str, login: str) -> str:
    # Older accounts can contain control characters; suggestions must remain valid names.
    login = "".join(char if char.isprintable() else "�" for char in login)
    occupied_names = {name for stand in project.stands for name in (stand.name, stand.title)}
    index = 0
    while True:
        suffix = f" - ({login})" + (f" - {index}" if index else "")
        candidate = base_name[:STAND_NAME_LIMIT - len(suffix)].rstrip() + suffix
        if candidate not in occupied_names:
            return candidate
        index += 1


@router.get("/projects/{code}/stand-instance-name")
def check_instance_name(code: str, name: str, response: Response, base_name: str | None = None,
                        _=Depends(creators), owner: User = Depends(cookie_user),
                        session: Session = Depends(get_session)):
    try:
        name = InstanceNameIn(name=name).name
        base_name = InstanceNameIn(name=base_name).name if base_name is not None else name
    except ValidationError:
        raise HTTPException(422, "неверное имя стенда") from None
    project = _project(session, code)
    conflict = _name_conflict(project, name)
    response.headers["Cache-Control"] = "no-store"
    return {"conflict": None if conflict is None else {
        "name": name, "stand_id": conflict.name,
        "suggested_name": _suggest_name(project, base_name, owner.login)}}


def _prepare(session: Session, code: str, body: InstanceIn, principal: Principal):
    project = _project(session, code)
    for value in (body.template_key, body.target_host, body.profile, *body.environments):
        _id(value)
    if len(set(body.environments)) != len(body.environments):
        raise HTTPException(422, "окружения повторяются")
    if principal.role == DEVELOPER and body.mode != "ephemeral":
        raise HTTPException(403, "разработчику доступны только временные стенды")
    if body.mode == "permanent" and body.expires is not None:
        raise HTTPException(422, "постоянному стенду не нужен срок окончания")
    if body.mode == "ephemeral" and (body.expires is None or body.expires <= datetime.now(UTC)):
        raise HTTPException(422, "временному стенду нужен будущий срок окончания")
    row = session.get(StandTemplate, (code, body.template_key, body.template_version))
    if row is None:
        raise HTTPException(422, "версия шаблона не найдена")
    template = copy.deepcopy(row.spec)
    repositories = {repo.key: {"components": repo.components} for repo in session.scalars(select(StandRepository).where(
        StandRepository.project == code, StandRepository.enabled.is_(True)))}
    request = {"schema_version": 1, "profile": body.profile,
               "components": {key: value.model_dump(exclude_none=True) for key, value in body.components.items()},
               "parameters": body.parameters}
    try:
        validate_template(template)
        for component in template["components"]:
            if component["id"] not in (row.technical_components or []):
                continue
            choice = request["components"].get(component["id"], {})
            if (choice.get("repository", component["repository"]) != component["repository"] or
                    choice.get("ref", component["default_ref"]) != component["default_ref"]):
                raise HTTPException(422, f"репозиторий и ref технического компонента {component['id']} заданы шаблоном")
        selected, parameters = validate_request(template, request, repositories=repositories)
    except (StandError, ValueError, TypeError, RecursionError):
        raise HTTPException(422, "шаблон, компоненты, репозитории, refs или параметры не прошли проверку") from None
    if template["secret_refs"]:
        raise HTTPException(422, "исполнитель ссылок на секреты ещё не подключён")
    host = _host(body.target_host)
    observed = []
    for component in selected:
        try:
            sha = resolve_ref(host, code, component["repository"], component["requested_ref"])
        except CatalogError:
            raise HTTPException(422, "ref отсутствует в разрешённом локальном репозитории") from None
        observed.append({**component, "observed_sha": sha})
    if _name_conflict(project, body.name) is not None:
        raise HTTPException(409, "имя стенда уже используется")
    if set(body.environments) & set(project.environments):
        raise HTTPException(409, "окружение уже зарегистрировано в проекте")
    # Some legacy stand environments are not included in project.environments.
    if any(set(body.environments) & set(stand.environments) for stand in project.stands):
        raise HTTPException(409, "окружение уже принадлежит стенду")
    mapping, allocations = _ports(session, host, template, body.ports)
    return project, template, selected, parameters, host, observed, mapping, allocations


def _latest_job(session, instance):
    return session.scalar(select(Job).where(Job.project == instance.project, Job.kind == "stand-build",
                                           Job.target == instance.id).order_by(Job.id.desc()).limit(1))


def executor_available(session, host):
    return session.scalar(select(ApiToken.name).where(ApiToken.role == "agent", ApiToken.active.is_(True),
        ApiToken.stand_host == host, ApiToken.last_seen >= now() - timedelta(seconds=60)).limit(1)) is not None


def waiting_reason(session, host):
    return "Ожидает свободного исполнителя шаблонов stand-template-v1" if executor_available(session, host) else WAITING


def _deletion_job(session, row):
    return session.scalar(select(Job).where(Job.project == row.project, Job.kind == "stand-delete",
                                           Job.target == row.id).order_by(Job.id.desc()).limit(1))


def _stand_locked(session, project, row):
    pending = session.scalars(select(Job).where(Job.project == row.project, Job.status.in_(("queued", "running"))))
    return any(locks(job) and lock_key(project, job) == f"{row.project}:stand:{row.id}" for job in pending)


def _queue_deletion(session, row, *, created_by, title):
    workspace_required = session.scalar(select(StandBuild.id).where(
        StandBuild.project == row.project, StandBuild.stand_id == row.id).limit(1)) is not None
    session.add(Job(project=row.project, kind="stand-delete", status="queued", target=row.id,
        title=title, created_by=created_by, created=now(),
        message="Ожидает исполнителя удаления stand-template-v1-delete",
        spec={"schema_version": 1, "project": row.project, "stand": row.id, "target_host": row.target_host,
              "lock": True, "workspace_required": workspace_required}))


def _instance_out(session, row, principal=None):

    job = _latest_job(session, row)
    deletion = _deletion_job(session, row)
    record = session.get(StandTemplate, (row.project, row.template_key, row.template_version))
    deleting = deletion is not None and deletion.status in ("queued", "running")
    return {"id": row.id, "name": row.name, "project": row.project, "owner_login": row.owner_login,
            "status": "deleted" if row.deleted_at else "deleting" if deleting else row.status,
            "target_host": row.target_host, "template_key": row.template_key,
            "template_version": row.template_version, "profile": row.profile, "environments": row.environments,
            "mode": row.mode, "expires": row.expires.replace(tzinfo=UTC) if row.expires else None,
            "job_id": job.id if job else None, "job_status": job.status if job else None,
            "ports": dict(job.spec.get("ports", {})) if job and not row.deleted_at else {},
            "job_message": job.message if job else None,
            "data_bundle": public_bundle(job.spec.get("test_data")) if job and principal and principal.role in (ADMIN, TESTER, DEVELOPER) else None,
            "deployed_build_id": row.deployed_build_id,
            "output_binding_titles": binding_titles(record.spec, record.output_binding_titles) if record else {},
            "deletion_job_id": deletion.id if deletion else None,
            "deletion_status": deletion.status if deletion else None,
            "deletion_message": deletion.message if deletion else None,
            "deleted_at": row.deleted_at.replace(tzinfo=UTC) if row.deleted_at else None,
            "can_delete": bool(principal and not row.deleted_at and not deleting and
                               (principal.role == ADMIN or principal.name == row.owner_login)),
            "waiting_reason": waiting_reason(session, row.target_host) if job and job.status == "queued" else None}


@router.post("/projects/{code}/stand-instances/preflight")
def preflight(code: str, body: InstanceIn, principal: Principal = Depends(creators), session: Session = Depends(get_session)):
    _, template, _, _, host, observed, ports, _ = _prepare(session, code, body, principal)
    available = executor_available(session, host.id)
    record = session.get(StandTemplate, (code, body.template_key, body.template_version))
    bundle = applicable_bundle(session, code, template, body.profile, body.install_test_data)
    return {"valid": True, "components": observed, "resources": copy.deepcopy(template["profiles"][body.profile]["resources"]),
            "ports": ports, "output_binding_titles": binding_titles(template, record.output_binding_titles),
            "data_bundle": bundle,
            "executor_available": available, "warnings": [] if available else [WAITING]}


@router.post("/projects/{code}/stand-instances", status_code=201)
def create_instance(code: str, body: InstanceIn, response: Response,
                    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
                    principal: Principal = Depends(creators), owner: User = Depends(cookie_user),
                    session: Session = Depends(get_session)):
    if not idempotency_key.strip() or not idempotency_key.isprintable():
        raise HTTPException(422, "неверный ключ идемпотентности")
    try:
        request_payload = body.model_dump(mode="json", exclude={"install_test_data"} if body.install_test_data else None)
        digest = hashlib.sha256(canonical({"body": request_payload, "owner": owner.login})).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HTTPException(422, "неверный JSON запроса") from None
    delete_after = _end_of_day(body.expires) if body.expires is not None else None
    _mutation_lock(session)
    try:
        existing = session.scalar(select(StandInstance).where(StandInstance.project == code,
                                                              StandInstance.request_key == idempotency_key))
        if existing is not None:
            if (existing.owner_login != owner.login or existing.request_hash != digest
                    or (existing.delete_after is not None and existing.delete_after != delete_after)):
                raise HTTPException(409, "ключ идемпотентности уже использован")
            response.status_code = 200
            result = _instance_out(session, existing, principal)
            session.rollback()
            return result
        project, template, selected, parameters, host, _, ports, allocations = _prepare(session, code, body, principal)
        bundle = applicable_bundle(session, code, template, body.profile, body.install_test_data)
        ensure_project(session, project)
        moment = now()
        row = StandInstance(id="stand-" + uuid4().hex, project=code, name=body.name,
            template_key=body.template_key, template_version=body.template_version, owner_login=owner.login,
            target_host=host.id, mode=body.mode,
            expires=body.expires.astimezone(UTC).replace(tzinfo=None) if body.expires is not None else None,
            delete_after=delete_after,
            profile=body.profile, refs={component["id"]: {"repository": component["repository"], "ref": component["requested_ref"]}
                                      for component in selected}, environments=body.environments,
            parameters=parameters, resources=copy.deepcopy(template["profiles"][body.profile]["resources"]),
            status="waiting", created=moment, updated=moment, request_key=idempotency_key, request_hash=digest)
        # The UUID must not hide an existing YAML identity; final registry validation runs after flush.
        if any(row.id in (stand.name, stand.title) for stand in project.stands):
            raise HTTPException(409, "идентификатор стенда уже используется")
        session.add(row)
        session.flush()
        for port in allocations:
            session.add(StandPortReservation(stand_id=row.id, target_host=host.id, protocol="tcp", port=port))
        job = Job(project=code, kind="stand-build", status="queued", target=row.id,
            title="Создать стенд: " + body.name, created_by=owner.login, created=moment, message=WAITING,
            spec={"schema_version": 1, "stand": row.id, "project": code, "template": template,
                  "template_sha256": template_hash(template), "request": {"schema_version": 1,
                      "profile": body.profile, "components": copy.deepcopy(row.refs), "parameters": parameters},
                  "target_host": host.id, "environments": list(body.environments), "ports": ports, "lock": True,
                  **({"test_data": bundle} if bundle["entries"] else {}),
                  **({"publish_ip": host.publish_ip} if host.publish_ip != "127.0.0.1" else {})})
        session.add(job)
        session.flush()
        _project(session, code)
        result = _instance_out(session, row, principal)
        _commit(session)
        return result
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "конфликт каталога, имени, запроса или порта") from None
    except Exception:
        session.rollback()
        raise


@router.get("/projects/{code}/stand-instances/{instance_id}")
def get_instance(code: str, instance_id: str, principal: Principal = Depends(readers), session: Session = Depends(get_session)):
    _project(session, code)
    row = session.get(StandInstance, instance_id)
    if row is None or row.project != code:
        raise HTTPException(404, "стенд не найден")
    return _instance_out(session, row, principal)


@router.delete("/projects/{code}/stand-instances/{instance_id}", status_code=202)
def delete_instance(code: str, instance_id: str, principal: Principal = Depends(readers),
                    owner: User = Depends(cookie_user), session: Session = Depends(get_session)):
    from .jobs import _claim_lock

    _mutation_lock(session)
    _claim_lock(session)
    try:
        row = session.get(StandInstance, instance_id)
        if row is None or row.project != code:
            raise HTTPException(404, "управляемый стенд не найден")
        if principal.role != ADMIN and row.owner_login != owner.login:
            raise HTTPException(403, "удаление разрешено администратору или создателю стенда")
        previous = _deletion_job(session, row)
        if row.deleted_at or previous and previous.status in ("queued", "running"):
            result = _instance_out(session, row, principal)
            session.rollback()
            return result
        project = _project(session, code)
        if _stand_locked(session, project, row):
            raise HTTPException(409, "стенд занят или ожидает выполнения задачи")
        _queue_deletion(session, row, created_by=owner.login, title="Удалить стенд: " + row.name)
        session.flush()
        result = _instance_out(session, row, principal)
        _commit(session)
        return result
    except Exception:
        session.rollback()
        raise
