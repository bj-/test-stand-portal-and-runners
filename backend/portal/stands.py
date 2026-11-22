"""Стенды: описание из манифеста установки и действия через очередь задач (ADR 0002, п. 2, 5).

Портал не знает устройство стенда: действие - команда из манифеста, её выполняет агент с возможностью stand-host.
Пользователь передаёт только значения параметров; они проверяются и уходят агенту переменными окружения.
"""
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import ADMIN, DEVELOPER, READERS, TESTER, USER_ROLES, VIEWER, Principal, now, require, require_read
from .config import ProjectConfig, StandConfig
from .db import get_session
from .models import Job, Project, Run
from .registry import RegistryError, load_registry
from .stand_contract import LIVENESS_STAGE

router = APIRouter(prefix="/api")


@dataclass(frozen=True)
class Action:
    title: str
    roles: tuple[str, ...]
    lock: bool  # занимает стенд: не идёт одновременно с прогоном и другими такими действиями
    params: bool = False
    destructive: bool = False


# Набор действий фиксирован в портале: что делает каждое - решает команда в манифесте.
ACTIONS: dict[str, Action] = {
    "deploy": Action("Пересобрать и развернуть", (ADMIN, TESTER), lock=True, params=True),
    "restart": Action("Перезапустить", (ADMIN, TESTER), lock=True),
    "seed": Action("Засеять тестовые данные", (ADMIN, TESTER), lock=True),
    "reset": Action("Сбросить данные", (ADMIN, TESTER), lock=True, destructive=True),
    "status": Action("Состояние", (ADMIN, TESTER, DEVELOPER), lock=False),
    "logs": Action("Логи", (ADMIN, TESTER, DEVELOPER), lock=False),
}
SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
SAFE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RECENT = 5

people = Depends(require(*USER_ROLES))


class LinkOut(BaseModel):
    title: str
    url: str


class ParamOut(BaseModel):
    name: str
    title: str
    default: str


class ActionOut(BaseModel):
    name: str
    title: str
    destructive: bool
    allowed: bool  # может ли текущий пользователь


class StandJob(BaseModel):
    id: int
    title: str
    status: str
    created_by: str
    created: datetime
    finished: datetime | None

    @field_validator("created", "finished")
    @classmethod
    def as_utc(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value else None


class AccessOut(BaseModel):
    title: str
    value: str
    secret: bool


class RunBrief(BaseModel):
    id: str
    env: str
    suite: str
    kind: str | None
    started: datetime | None
    tests: int
    passed: int
    failed: int
    errors: int
    gate_failed: bool


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class Health(BaseModel):
    state: str  # ok, limited, broken, unknown
    reasons: list[str]
    checks: list[Check]
    checked: str | None


class StandOut(BaseModel):
    name: str
    title: str
    source: str
    state: str | None
    mode: str | None
    expires: datetime | None
    can_run: bool
    description: str  # Markdown
    problem: str  # Markdown, известная проблема
    seed_info: str  # Markdown, что засевает действие seed; скрыто от viewer и гостя
    can_view_test_data: bool
    environments: list[str]
    links: list[LinkOut]
    access: list[AccessOut] | None  # None - роли не положено
    info: dict | None  # сведения о сборке от самого стенда: built, deployed, components, migrations
    health: Health
    runs: list[RunBrief]  # последний прогон на каждом окружении стенда
    params: list[ParamOut]
    actions: list[ActionOut]
    busy: bool  # выполняется прогон или действие, занимающее стенд
    jobs: list[StandJob]


class ActionIn(BaseModel):
    params: dict[str, str] = {}
    confirm: bool = False  # явное подтверждение разрушающего действия


def _project(session: Session, code: str) -> ProjectConfig:
    try:
        return load_registry(session).get(code)
    except RegistryError as error:
        raise HTTPException(409, str(error)) from None
    except KeyError:
        raise HTTPException(404, "проект не найден") from None


def require_executor(stand: StandConfig | None) -> None:
    if stand is not None and stand.source == "database":
        raise HTTPException(409, "исполнение управляемого стенда ещё не подключено")


def ensure_project(session: Session, project: ProjectConfig) -> None:
    """Создать индекс YAML-проекта при разрешённой записи; чтение реестра БД не меняет."""
    if session.get(Project, project.code) is not None:
        return
    try:
        with session.begin_nested():
            session.add(Project(code=project.code, title=project.title))
            session.flush()
    except IntegrityError:
        # Другой POST мог создать индекс между чтением и INSERT.
        if session.get(Project, project.code) is None:
            raise


def _stand(project: ProjectConfig, name: str) -> StandConfig:
    stand = next((s for s in project.stands if s.name == name), None)
    if stand is None:
        raise HTTPException(404, "стенд не найден")
    return stand


def lock_key(project: ProjectConfig | None, job: Job) -> str:
    """Что занимает задача: стенд целиком. Прогон занимает стенд своего окружения, без стенда - само окружение."""
    if job.kind in {"stand", "stand-build", "stand-delete", "stand-control"} or (job.kind == "run" and (job.spec or {}).get("stand_target")):
        return f"{job.project}:stand:{job.target}"
    stand = project.stand_of(job.target) if project else None
    return f"{job.project}:stand:{stand}" if stand else f"{job.project}:env:{job.target}"


def locks(job: Job) -> bool:
    return job.kind not in {"stand", "stand-control"} or bool((job.spec or {}).get("lock"))


def _stand_jobs(session: Session, project: str, stand: StandConfig) -> list[Job]:
    query = select(Job).where(Job.project == project, Job.kind.in_(("stand", "stand-build", "stand-delete", "stand-control")), Job.target == stand.name)
    return list(session.scalars(query.order_by(Job.id.desc()).limit(RECENT)))


def deleting_stands(session: Session, project: str) -> set[str]:
    return set(session.scalars(select(Job.target).where(
        Job.project == project, Job.kind == "stand-delete", Job.status.in_(("queued", "running")))))


def _busy(session: Session, project: ProjectConfig, stand: StandConfig) -> bool:
    key = f"{project.code}:stand:{stand.name}"
    running = session.scalars(select(Job).where(Job.project == project.code, Job.status == "running"))
    return any(locks(job) and lock_key(project, job) == key for job in running)


PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _secrets(stand: StandConfig) -> dict[str, str]:
    values: dict[str, str] = {}
    for file in stand.secrets_files:
        try:
            lines = Path(file).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def _access(stand: StandConfig, role: str) -> list[AccessOut] | None:
    if role not in stand.access_roles:
        return None
    values = _secrets(stand)
    return [
        AccessOut(title=a["title"], value=PLACEHOLDER.sub(lambda m: values.get(m.group(1), "?"), a["value"]), secret=a["secret"])
        for a in stand.access
    ]


def _info(stand: StandConfig) -> dict | None:
    if stand.source == "database":
        return stand.build_info
    if not stand.info_file:
        return None
    try:
        data = json.loads(Path(stand.info_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def product_on(stand: StandConfig | None) -> dict | None:
    """Что развёрнуто на стенде окружения - для прогона (--build, --product-commit движка): версии и коммиты."""
    info = _info(stand) if stand else None
    components = [c for c in (info or {}).get("components") or [] if isinstance(c, dict) and c.get("name")]
    if not components:
        return None
    versions = ", ".join(f"{c['name']} {c.get('api_version') or c.get('version') or '?'}" for c in components)
    return {
        "build": f"{stand.title}: {versions}",
        "commits": {c["name"]: c["commit"] for c in components if c.get("commit")},
    }


def _naive(value: str | None) -> datetime | None:
    """Время стенда - местное с поясом; время прогонов в индексе - местное без пояса."""
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None) if value else None
    except ValueError:
        return None


def _runs(session: Session, project: str, stand: StandConfig) -> list[Run]:
    latest = []
    for env in stand.environments:
        query = select(Run).where(Run.project == project, Run.stand_name == env, Run.started.is_not(None))
        run = session.scalars(query.order_by(Run.started.desc()).limit(1)).first()
        if run is not None:
            latest.append(run)
    return latest


def _health(stand: StandConfig, info: dict | None, runs: list[Run]) -> Health:
    """Состояние развёрнутой сборки, проверки и последние прогоны на стенде."""
    reasons: list[str] = []
    verification: list[str] = []
    managed = stand.source == "database"
    failed = managed and stand.state == "failed"
    liveness = stand.verification.get(LIVENESS_STAGE)
    if managed and stand.state in ("limited", "failed") and stand.verification.get("smoke") == "failed":
        verification.append("Smoke не пройден.")
        verification.append({"success": "Отклик стенда подтверждён.",
                             "failed": "Отклик стенда не подтверждён."}.get(liveness, "Отклик стенда не проверялся."))
    if stand.problem:
        reasons.append("известная проблема")
    if failed and (not verification or liveness == "success"):
        reasons.append("последняя сборка завершилась ошибкой или была отменена")
    checks = [Check(**c) for c in (info or {}).get("health") or [] if isinstance(c, dict) and "name" in c]
    reasons += [f"{c.name}: {c.detail}" for c in checks if not c.ok and not (managed and c.name == "smoke")]
    deployed = _naive((info or {}).get("deployed"))
    fresh = [run for run in runs if not deployed or (run.started and run.started >= deployed)]
    for run in fresh:
        if run.gate_failed:
            reasons.append(f"{run.stand_name}: прогон {run.id} не прошёл gate (смоук в начале прогона)")
        elif run.kind == "smoke" and (run.failed or run.errors):
            reasons.append(f"{run.stand_name}: смоук {run.id} - упало {run.failed + run.errors} из {run.tests}")
    if failed or reasons:
        state = "broken"
    elif managed and stand.state == "limited":
        state = "limited"
    elif managed and stand.state != "ready":
        state = "unknown"
    elif checks and (fresh or (managed and stand.build_info)):
        state = "ok"
    else:
        state = "unknown"  # нет проверки или нет прогона после развёртывания
    return Health(state=state, reasons=verification + reasons, checks=checks, checked=(info or {}).get("checked"))


@router.get("/projects/{code}/stands", response_model=list[StandOut])
# Ассистенту (llm) стенды видны для разбора прогонов - без доступов: llm нет в access_roles.
def stands(code: str, request: Request, principal: Principal = Depends(require_read(*READERS)), session: Session = Depends(get_session)):
    project = _project(session, code)
    deleting = deleting_stands(session, code)
    result = []
    for stand in project.stands:
        from .stand_control import managed_actions

        info = _info(stand)
        runs = _runs(session, code, stand)
        result.append(StandOut(
            name=stand.name,
            title=stand.title,
            source=stand.source,
            state=stand.state,
            mode=stand.mode,
            expires=stand.expires,
            can_run=not principal.is_guest and principal.role in (ADMIN, TESTER, DEVELOPER)
            and stand.name not in deleting and project.can_run_stand(stand),
            description=stand.description,
            problem=stand.problem,
            seed_info=stand.seed_info if not principal.is_guest and principal.role != VIEWER else "",
            can_view_test_data=not principal.is_guest and principal.role != VIEWER,
            environments=stand.environments,
            links=[LinkOut(**link) for link in stand.links],
            access=_access(stand, principal.role),
            info=info,
            health=_health(stand, info, runs),
            runs=[RunBrief(id=r.id, env=r.stand_name or "", suite=r.suite, kind=r.kind, started=r.started, tests=r.tests,
                           passed=r.passed, failed=r.failed, errors=r.errors, gate_failed=r.gate_failed) for r in runs],
            params=[ParamOut(name=p.name, title=p.title, default=p.default) for p in stand.params],
            actions=managed_actions(session, code, stand, principal, request) if stand.source == "database" else [
                ActionOut(name=name, title=action.title, destructive=action.destructive, allowed=principal.role in action.roles)
                for name, action in ACTIONS.items() if name in stand.commands
            ],
            busy=_busy(session, project, stand),
            jobs=[StandJob.model_validate(job, from_attributes=True) for job in _stand_jobs(session, code, stand)],
        ))
    return result


@router.post("/projects/{code}/stands/{name}/{action}", status_code=201)
def stand_action(
    code: str, name: str, action: str, data: ActionIn, request: Request,
    principal: Principal = Depends(require(*USER_ROLES)), session: Session = Depends(get_session),
) -> dict:
    project = _project(session, code)
    stand = _stand(project, name)
    if stand.source == "database":
        from .stand_control import queue_action

        return queue_action(session, project, stand, action, data, principal, request)
    require_executor(stand)
    spec_action = ACTIONS.get(action)
    if spec_action is None or action not in stand.commands:
        raise HTTPException(404, "у стенда нет такого действия")
    if principal.role not in spec_action.roles:
        raise HTTPException(403, "недостаточно прав для этого действия")
    if spec_action.destructive and not data.confirm:
        raise HTTPException(422, "действие разрушающее: нужно явное подтверждение")
    if not stand.workdir:
        raise HTTPException(422, "у стенда не задан каталог команд (workdir)")

    env: dict[str, str] = {}
    if spec_action.params:
        known = {p.name: p for p in stand.params}
        unknown = set(data.params) - set(known)
        if unknown:
            raise HTTPException(422, f"неизвестные параметры: {', '.join(sorted(unknown))}")
        for param in stand.params:
            value = (data.params.get(param.name) or param.default).strip()
            if not value:
                continue
            if not SAFE_NAME.match(param.name) or not SAFE_VALUE.match(value) or ".." in value:
                raise HTTPException(422, f"недопустимое значение: {param.title}")
            env[param.name] = value
    elif data.params:
        raise HTTPException(422, "у действия нет параметров")

    details = ", ".join(f"{key}={value}" for key, value in env.items())
    ensure_project(session, project)
    job = Job(
        project=project.code,
        kind="stand",
        target=stand.name,
        title=f"{spec_action.title}: {stand.title}" + (f" ({details})" if details else ""),
        created_by=principal.name,
        created=now(),
        status="queued",
        cancel_requested=False,
        spec={
            "project": project.code,
            "stand": stand.name,
            "action": action,
            "workdir": stand.workdir,
            "command": stand.commands[action],
            "env": env,
            "lock": spec_action.lock,
            "requested_by": principal.name,
        },
    )
    session.add(job)
    if action in {"deploy", "reset", "seed"}:
        from .test_data import snapshot_bindings

        snapshot_bindings(session, job, stand)
    session.commit()
    return {"id": job.id}
