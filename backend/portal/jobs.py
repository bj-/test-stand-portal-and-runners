"""Очередь задач (ADR 0002, п. 2-3). Портал только ведёт задачи, исполняют агенты: сами забирают работу по HTTP."""
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import ADMIN, AGENT, DEVELOPER, TESTER, USER_ROLES, Principal, now, require, require_read
from .config import ProjectConfig, StandConfig, artifacts_dir
from .db import get_session
from .models import ApiToken, Job, JobLog, StandBuild, StandInstance
from .registry import RegistryError, load_registry
from .stand_contract import DATA_CAPABILITY, LIVENESS_CAPABILITY, LIVENESS_STAGE
from .stands import _project, deleting_stands, ensure_project, lock_key, locks, product_on, require_executor
from .test_catalog import suite_metadata, test_catalog

router = APIRouter(prefix="/api")

LOST_AFTER = timedelta(minutes=5)
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
SAFE_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,79}$")
# Подстрока пути теста для точечного запуска: печатные символы, в т. ч. кириллица и пробелы; уходит движку как --test=...
SAFE_TEST = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
MAX_TESTS = 30
SAFE_RUN_ID = re.compile(r"^[A-Z0-9]+-\d{8}-\d{6}-[A-Za-z0-9._-]+$")
ARTIFACTS = {"allure-report.html"}
MAX_ARTIFACT = 64 * 1024 * 1024
JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATES = ("done", "failed", "cancelled")
# Единая короткая секция выдачи между всеми процессами портала (не блокировка исполнения).
CLAIM_LOCK_NAMESPACE = 0x54504F52
CLAIM_LOCK_KEY = 1
MANAGED_RUN_CAPABILITY = "managed-newman-v1"

people = Depends(require_read(*USER_ROLES))
runners = require(ADMIN, TESTER, DEVELOPER)
agents = require(AGENT)


class SuiteOut(BaseModel):
    path: str
    description: str
    destructive: bool


class RunBindingOut(BaseModel):
    id: str
    title: str


class RunStandOut(BaseModel):
    id: str
    name: str
    status: str
    bindings: list[RunBindingOut]


class CatalogOut(BaseModel):
    suites: list[SuiteOut]
    environments: list[str]
    legacy_environments: list[str]
    run_stands: list[RunStandOut]


class RunOptions(BaseModel):
    project: str
    env: str
    stand_id: str | None = Field(default=None, min_length=1, max_length=128)
    ref: str = "main"
    tags: list[str] = []
    groups: list[str] = []
    destructive: bool = False  # только по явному решению тестировщика


class SuiteSelection(BaseModel):
    suite: str
    tests: list[str] = []  # точечный запуск: подстроки пути теста
    test_ids: list[str] | None = None  # None — весь набор, [] — ошибка, иначе точные ID


class JobIn(RunOptions, SuiteSelection):
    pass


class JobBatchIn(RunOptions):
    suites: list[SuiteSelection] = Field(min_length=1, max_length=20)

    @field_validator("suites")
    @classmethod
    def unique_suites(cls, value):
        if len({item.suite for item in value}) != len(value):
            raise ValueError("наборы в пакете не должны повторяться")
        return value


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_id: str | None
    project: str
    kind: str
    status: str
    target: str
    title: str
    created_by: str
    created: datetime
    started: datetime | None
    finished: datetime | None
    agent: str | None
    run_id: str | None
    message: str | None
    cancel_requested: bool

    @field_validator("created", "started", "finished")
    @classmethod
    def as_utc(cls, value: datetime | None) -> datetime | None:
        """В БД - UTC без пояса; без пометки браузер принял бы его за местное время."""
        return value.replace(tzinfo=UTC) if value else None


class JobDetail(JobOut):
    log: str


class JobPage(BaseModel):
    total: int
    items: list[JobOut]


class JobBatchOut(BaseModel):
    id: str
    project: str
    created_by: str
    created: datetime
    total: int
    finished: int
    status: str
    counts: dict[str, int]
    jobs: list[JobOut]


class ClaimIn(BaseModel):
    capabilities: list[str]
    host_id: str | None = None


class ClaimOut(BaseModel):
    id: int
    kind: str
    spec: dict


class LogIn(BaseModel):
    text: str


class LogOut(BaseModel):
    cancel: bool


class FinishIn(BaseModel):
    status: str
    run_id: str | None = None
    message: str | None = None


def _catalog(project: ProjectConfig, deleting: set[str] | None = None) -> CatalogOut:
    suites = []
    if project.tests:
        root = project.tests / "suites"
        for file in sorted(root.rglob("*.yaml")):
            data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            suites.append(SuiteOut(
                path=file.relative_to(project.tests).as_posix(),
                description=data.get("description") or "",
                destructive=data.get("only") == "destructive",
            ))
    managed_envs = {env for stand in project.stands if stand.source == "database" for env in stand.environments}
    return CatalogOut(
        suites=suites, environments=project.environments,
        legacy_environments=[env for env in project.environments if env not in managed_envs],
        run_stands=[
            RunStandOut(id=stand.name, name=stand.title, status=stand.state, bindings=[
                RunBindingOut(id=name, title=stand.output_binding_titles.get(name, name)) for name in stand.output_bindings
            ])
            for stand in sorted(project.stands, key=lambda s: s.title.casefold())
            if project.can_run_stand(stand) and stand.name not in (deleting or ())
        ],
    )


def _suite_tests(project: ProjectConfig, suite_path: str) -> list[str]:
    """Названия тестов коллекции набора - подсказки для точечного запуска (файлы <имя>.request.yaml коллекции v3)."""
    if not project.tests:
        return []
    file = project.tests / suite_path
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    collection = project.tests / "postman" / "collections" / str(data.get("collection", ""))
    if not data.get("collection") or not collection.is_dir():
        return []
    return sorted({p.name.removesuffix(".request.yaml") for p in collection.rglob("*.request.yaml")})


def _log_text(session: Session, job_id: int) -> str:
    return "".join(session.scalars(select(JobLog.text).where(JobLog.job_id == job_id).order_by(JobLog.id)))


@router.get("/projects/{code}/catalog", response_model=CatalogOut, dependencies=[people])
def catalog(code: str, session: Session = Depends(get_session)):
    return _catalog(_project(session, code), deleting_stands(session, code))


@router.get("/projects/{code}/catalog/branches", response_model=list[str], dependencies=[people])
def catalog_branches(code: str, session: Session = Depends(get_session)):
    """Отсортированные имена веток локального зеркала тестов, без повторов и символических refs.

    Источники — локальные ветки и origin; теги и имена, недопустимые для запуска,
    исключаются. Для дерева без git возвращается main. Обновление refs выполняет
    синхронизация установки. Недоступный репозиторий или ошибка git дают 422.
    """
    root = _project(session, code).tests
    if not root or not root.is_dir():
        raise HTTPException(422, "репозиторий тестов недоступен")
    if not (root / ".git").exists():
        return ["main"]  # Как и каталог тестов, дерево без git доступно только для main.
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root.resolve()}", "-C", str(root),
             "for-each-ref", "--format=%(refname)\t%(symref)", "refs/heads/", "refs/remotes/origin/"],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HTTPException(422, "не удалось прочитать ветки репозитория тестов") from error
    branches = set()
    for line in result.stdout.splitlines():
        ref, _, symbolic = line.partition("\t")
        if symbolic:
            continue
        name = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref.removeprefix("refs/remotes/origin/")
        if SAFE_REF.fullmatch(name) and ".." not in name:
            branches.add(name)
    return sorted(branches)


@router.get("/projects/{code}/catalog/tests", response_model=list[str], dependencies=[people])
def catalog_tests(code: str, suite: str, session: Session = Depends(get_session)):
    project = _project(session, code)
    if suite not in {s.path for s in _catalog(project).suites}:
        raise HTTPException(404, "набора нет в репозитории тестов")
    return _suite_tests(project, suite)


def _exact_catalog(project, suite, ref):
    return _ref_catalog(project, suite, ref, exact=True)


def _ref_catalog(project, suite, ref, *, exact=False):
    if not SAFE_REF.fullmatch(ref) or ".." in ref:
        raise HTTPException(422, "недопустимая ветка")
    if not project.tests:
        raise HTTPException(422, "репозиторий тестов недоступен")
    try:
        return (test_catalog(project.tests, project.runner, suite, ref) if exact
                else suite_metadata(project.tests, suite, ref))
    except (ValueError, OSError, KeyError, yaml.YAMLError, SyntaxError, subprocess.SubprocessError) as error:
        raise HTTPException(422, "каталог недоступен: проверьте ветку, путь набора и формат файлов репозитория") from error


@router.get("/projects/{code}/catalog/test-ids", dependencies=[people])
def catalog_test_ids(code: str, suite: str, ref: str = "main", session: Session = Depends(get_session)):
    return _exact_catalog(_project(session, code), suite, ref)


def _run_stand(project: ProjectConfig, stand_id: str, binding: str) -> StandConfig:
    stand = next((stand for stand in project.stands if stand.source == "database" and stand.name == stand_id), None)
    if stand is None:
        raise HTTPException(404, "стенд не найден")
    if not project.can_run_stand(stand):
        raise HTTPException(409, "стенд или исполнитель проекта пока не готов к прогону")
    if binding not in stand.output_bindings:
        raise HTTPException(422, "сервис не опубликован на выбранном стенде")
    return stand


def validate_run(project: ProjectConfig, data: JobIn, principal: Principal) -> list[str]:
    """Общий для одиночного запуска и пакета этап, ничего не ставит в очередь."""
    # Окружения - белый список установки: так PROD не запустить из формы.
    if data.stand_id is None:
        if data.env not in project.environments:
            raise HTTPException(422, "окружение не разрешено для запуска из портала")
        require_executor(next((stand for stand in project.stands if data.env in stand.environments), None))
    else:
        _run_stand(project, data.stand_id, data.env)
    if not SAFE_REF.fullmatch(data.ref) or ".." in data.ref:
        raise HTTPException(422, "недопустимая ветка")
    if not all(SAFE_TAG.fullmatch(t) for t in [*data.tags, *data.groups]):
        raise HTTPException(422, "недопустимая метка")
    tests = [t.strip() for t in data.tests if t.strip()]
    if data.tests and not tests:
        raise HTTPException(422, "пустой фильтр тестов")
    if len(tests) > MAX_TESTS or not all(SAFE_TEST.fullmatch(t) for t in tests):
        raise HTTPException(422, f"отдельные тесты: до {MAX_TESTS} подстрок, без управляющих символов, до 120 знаков")
    if data.test_ids is not None:
        if data.tests:
            raise HTTPException(422, "test_ids и tests нельзя использовать вместе")
        if not data.test_ids or len(data.test_ids) > 500 or len(set(data.test_ids)) != len(data.test_ids):
            raise HTTPException(422, "выберите от 1 до 500 различных тестов")
    metadata = (_exact_catalog(project, data.suite, data.ref) if data.test_ids is not None
                else _ref_catalog(project, data.suite, data.ref))
    if data.test_ids is not None:
        if not set(data.test_ids) <= {item["id"] for item in metadata["items"]}:
            raise HTTPException(422, "выбранные тесты отсутствуют в каталоге набора и ветки")
    if (data.destructive or metadata["destructive"]) and principal.role not in (ADMIN, TESTER):
        raise HTTPException(403, "разрушающие тесты запускает тестировщик")
    if metadata["destructive"] and not data.destructive:
        raise HTTPException(422, "набор разрушающий: нужно явное подтверждение")
    return tests


def _run_target(session: Session, project: ProjectConfig, data: RunOptions) -> tuple[ProjectConfig, StandConfig | None]:
    if data.stand_id is None:
        return project, None
    # Синхронизируем постановку с удалением/пересборкой после чтения каталога тестов.
    from .managed_stands import _mutation_lock

    _mutation_lock(session)
    _claim_lock(session)
    project = _project(session, project.code)
    if data.stand_id in deleting_stands(session, project.code):
        raise HTTPException(409, "стенд удаляется")
    return project, _run_stand(project, data.stand_id, data.env)


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(data: JobIn, principal: Principal = Depends(runners), session: Session = Depends(get_session)):
    project = _project(session, data.project)
    tests = validate_run(project, data, principal)
    project, stand = _run_target(session, project, data)

    job = queue_run(session, project, data.suite, data.env, data.ref, principal.name,
                    tags=data.tags, groups=data.groups, destructive=data.destructive,
                    tests=tests, test_ids=data.test_ids, stand=stand)
    session.commit()
    return job


def _batch_out(batch_id: str, members: list[Job]) -> JobBatchOut:
    counts = Counter(job.status for job in members)
    finished = sum(counts[state] for state in TERMINAL_STATES)
    if counts["running"] or (counts["queued"] and finished):
        status = "running"
    elif counts["queued"]:
        status = "queued"
    else:
        status = next((state for state in TERMINAL_STATES if counts[state] == len(members)), "mixed")
    first = JobOut.model_validate(members[0])
    return JobBatchOut(
        id=batch_id, project=first.project, created_by=first.created_by, created=first.created,
        total=len(members), finished=finished, status=status,
        counts={state: counts[state] for state in JOB_STATES},
        jobs=[JobOut.model_validate(job) for job in members],
    )


@router.post("/job-batches", response_model=JobBatchOut, status_code=201)
def create_job_batch(data: JobBatchIn, principal: Principal = Depends(runners), session: Session = Depends(get_session)):
    project = _project(session, data.project)
    options = data.model_dump(exclude={"suites"})
    selections = [JobIn(**options, **suite.model_dump()) for suite in data.suites]
    # Валидация ВСЕГО пакета завершается до первого add/flush; commit общий для всех задач.
    validated = [(selection, validate_run(project, selection, principal)) for selection in selections]
    project, stand = _run_target(session, project, data)
    batch_id = str(uuid4())
    members = [queue_run(
        session, project, selection.suite, selection.env, selection.ref, principal.name,
        tags=selection.tags, groups=selection.groups, destructive=selection.destructive,
        tests=tests, test_ids=selection.test_ids, batch_id=batch_id, stand=stand,
    ) for selection, tests in validated]
    session.commit()
    return _batch_out(batch_id, members)


@router.get("/job-batches/{batch_id}", response_model=JobBatchOut, dependencies=[people])
def job_batch(batch_id: str, session: Session = Depends(get_session)):
    members = session.scalars(select(Job).where(Job.batch_id == batch_id).order_by(Job.id)).all()
    if not members:
        raise HTTPException(404, "пакет не найден")
    return _batch_out(batch_id, members)


def queue_run(session: Session, project: ProjectConfig, suite: str, env: str, ref: str, requested_by: str,
              tags: list[str] | None = None, groups: list[str] | None = None, destructive: bool = False,
              note: str = "", tests: list[str] | None = None, test_ids: list[str] | None = None,
              batch_id: str | None = None, stand: StandConfig | None = None) -> Job:
    if stand is None:
        require_executor(next((item for item in project.stands if env in item.environments), None))
    ensure_project(session, project)
    selected = test_ids or tests
    if selected:
        shown = ", ".join(selected[:3]) + (f" и ещё {len(selected) - 3}" if len(selected) > 3 else "")
        note = f"тесты: {shown}" + (f"; {note}" if note else "")
    target_title = f"{stand.title} · {stand.output_binding_titles.get(env, env)}" if stand else env
    title = f"{Path(suite).stem} / {target_title}" + (f" ({note})" if note else "")
    job = Job(
        batch_id=batch_id,
        project=project.code,
        kind="run",
        target=stand.name if stand else env,
        title=title[:300] if stand else title,
        created_by=requested_by,
        created=now(),
        status="queued",
        cancel_requested=False,
        spec={
            "project": project.code,
            "stand": stand.name if stand else project.stand_of(env),
            "tests_url": project.tests_url,
            "results_url": project.results_url,
            "suite": suite,
            "env": f"{stand.name}--{env}" if stand else env,
            "ref": ref,
            "tags": tags or [],
            "groups": groups or [],
            "tests": tests or [],
            "test_ids": test_ids,
            "destructive": destructive,
            "requested_by": requested_by,
            "runner": project.runner,
            **({"stand_target": {"host_id": stand.target_host, "binding": env}} if stand else {}),
        },
    )
    session.add(job)
    return job


SMOKE_AFTER = {"deploy", "restart", "reset"}


def _smoke_after(session: Session, job: Job) -> None:
    """Стенд пересобран, перезапущен или сброшен - сразу проверить смоуком: нерабочий стенд будет виден на карточке."""
    spec = job.spec or {}
    if job.kind != "stand" or spec.get("action") not in SMOKE_AFTER:
        return
    try:
        project = load_registry(session).get(job.project)
    except (RegistryError, KeyError):
        return
    stand = next((s for s in project.stands if s.name == job.target), None)
    if stand is None or stand.source == "database":
        return
    smoke = stand.smoke if stand else {}
    suite, env = smoke.get("suite"), smoke.get("env")
    if not suite or not env or env not in project.environments:
        return
    if any(s.source == "database" and env in s.environments for s in project.stands):
        return
    if not any(s.path == suite and not s.destructive for s in _catalog(project).suites):
        return
    queue_run(session, project, suite, env, smoke.get("ref", "main"), job.created_by, note=f"после задачи {job.id}")


@router.get("/jobs", response_model=JobPage, dependencies=[people])
def jobs(project: str | None = None, limit: int = 100, session: Session = Depends(get_session)):
    query = select(Job)
    if project:
        query = query.where(Job.project == project)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    return JobPage(total=total, items=session.scalars(query.order_by(Job.id.desc()).limit(min(limit, 200))).all())


@router.get("/jobs/{job_id}", response_model=JobDetail, dependencies=[people])
def job_detail(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "задача не найдена")
    return JobDetail(**JobOut.model_validate(job).model_dump(), log=_log_text(session, job_id))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, principal: Principal = Depends(runners), session: Session = Depends(get_session)):
    _claim_lock(session)
    job = session.scalars(select(Job).where(Job.id == job_id).with_for_update()).first()
    if job is None:
        raise HTTPException(404, "задача не найдена")
    if principal.role == DEVELOPER and job.created_by != principal.name:
        raise HTTPException(403, "разработчик отменяет только свои задачи")
    if job.status == "queued":
        job.status = "cancelled"
        job.finished = now()
        if job.kind == "stand-build":
            instance = session.get(StandInstance, job.target)
            if instance and instance.project == job.project and instance.status == "waiting":
                instance.status = "created"
            job.message = "Сборка отменена; экземпляр и резервации портов сохранены"
        elif job.kind == "stand-delete":
            job.message = "Удаление отменено; экземпляр и резервации портов сохранены"
    elif job.status == "running":
        job.cancel_requested = True
    _cancel_children(session, job.id)
    session.commit()
    return job


def _fail_lost_jobs(session: Session) -> None:
    """Агент пропал посреди задачи: иначе стенд остался бы занят навсегда."""
    moment = now()
    lost = select(Job).where(Job.status == "running", Job.heartbeat < moment - LOST_AFTER)
    for job in session.scalars(lost).all():
        job.status = "failed"
        job.finished = moment
        job.message = "агент перестал отвечать"
        if job.kind == "stand-build":
            build = session.scalar(select(StandBuild).where(StandBuild.job_id == job.id))
            if build:
                build.status, build.finished = "failed", moment
            instance = session.get(StandInstance, job.target)
            if instance and instance.project == job.project:
                instance.status = "failed"
        _cancel_children(session, job.id)
    session.flush()


def required_capability(project: ProjectConfig | None, job: Job) -> str | None:
    """Прогон - возможность исполнителя проекта (newman, appium-android...), действие со стендом - stand-host."""
    if job.kind == "stand-build":
        if LIVENESS_STAGE in (job.spec or {}).get("template", {}):
            return LIVENESS_CAPABILITY
        return "stand-template-v1"
    if job.kind == "stand-delete":
        return "stand-template-v1-delete"
    if job.kind == "stand-control":
        return "stand-template-v1-control"
    if job.kind == "stand":
        return "stand-host"
    return project.run_capability if project else None


@router.post("/agent/claim", response_model=ClaimOut | None)
def claim(data: ClaimIn, response: Response, principal: Principal = Depends(agents), session: Session = Depends(get_session)):
    return _claim(data, response, principal, session)


def _claim_lock(session):
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(CLAIM_LOCK_NAMESPACE, CLAIM_LOCK_KEY)))


def _cancel_children(session, parent_id):
    for child in session.scalars(select(Job).where(Job.parent_job_id == parent_id,
                                Job.status.in_(("queued", "running")))):
        if child.status == "queued":
            child.status, child.finished = "cancelled", now()
            child.message = "родительский прогон завершён или отменён"
        else:
            child.cancel_requested = True


@router.post("/agent/jobs/{parent_id}/data-claim", response_model=ClaimOut | None)
def claim_child(parent_id: int, data: ClaimIn, response: Response, principal: Principal = Depends(agents),
                session: Session = Depends(get_session)):
    # Этот endpoint не выдаёт произвольную работу фоновому обработчику агента.
    return _claim(data, response, principal, session, parent_id=parent_id)


def _claim(data, response, principal, session, parent_id=None):
    # READ COMMITTED: после ожидания lock следующий SELECT видит commit предыдущей выдачи.
    # SQLite не поддерживает эту гарантию; fallback предназначен для последовательных локальных тестов.
    _claim_lock(session)
    from .update_control import maintenance_active

    draining = maintenance_active()
    # Preserve active jobs (including stale heartbeats) throughout deployment.
    # Their child operations must still be claimable so runs can finish.
    if not draining:
        _fail_lost_jobs(session)
    registry = load_registry(session)
    token = session.scalar(select(ApiToken).where(ApiToken.name == principal.name,
        ApiToken.role == AGENT, ApiToken.active.is_(True)))
    bound_host = token.stand_host if token and token.stand_host == data.host_id else None
    if bound_host and set(data.capabilities) & {"stand-template-v1", "stand-template-v1-delete", "stand-template-v1-control", LIVENESS_CAPABILITY, DATA_CAPABILITY}:
        token.last_seen = now()
    projects = registry.projects
    key = lambda job: lock_key(projects.get(job.project), job)  # noqa: E731
    running = session.scalars(select(Job).where(Job.status == "running")).all()
    active = {job.id: job for job in running}
    if parent_id is not None:
        parent = active.get(parent_id)
        if (parent is None or parent.kind != "run" or parent.agent != principal.name or parent.cancel_requested
                or "stand-host" not in data.capabilities):
            session.commit()
            response.status_code = 204
            return None
    query = select(Job).where(Job.status == "queued").order_by(Job.id).with_for_update(skip_locked=True)
    if parent_id is not None:
        query = query.where(Job.parent_job_id == parent_id)
    waiting: set[str] = set()
    for job in session.scalars(query).all():
        if job.kind in ("stand-build", "stand-delete", "stand-control"):
            if not bound_host or bound_host != (job.spec or {}).get("target_host"):
                continue
            instance = session.get(StandInstance, job.target)
            if (instance is None or instance.project != job.project or instance.deleted_at is not None
                    or instance.target_host != bound_host):
                job.status, job.finished = "failed", now()
                job.message = "управляемый стенд недоступен"
                continue
            if job.kind == "stand-control" and job.target in deleting_stands(session, job.project):
                job.status, job.finished = "cancelled", now()
                job.message = "стенд ожидает или выполняет удаление"
                continue
            if job.kind == "stand-control" and (job.spec.get("stand") != instance.id or job.spec.get("project") != instance.project):
                job.status, job.finished, job.message = "failed", now(), "несогласованная операция стенда"
                continue
        project = projects.get(job.project)
        if project is None:
            job.message = registry.errors.get(job.project, "конфигурация проекта недоступна; задача ожидает настройки")
            continue
        run_target = (job.spec or {}).get("stand_target") if job.kind == "run" else None
        if run_target:
            if not bound_host or bound_host != run_target["host_id"] or MANAGED_RUN_CAPABILITY not in data.capabilities:
                continue
            try:
                stand = _run_stand(project, job.target, run_target["binding"])
                if stand.target_host != bound_host or stand.name in deleting_stands(session, job.project):
                    raise HTTPException(409, "стенд недоступен на хосте исполнителя")
            except HTTPException as error:
                job.status, job.finished, job.message = "failed", now(), str(error.detail)
                continue
        else:
            if job.kind == "run" and job.target not in project.environments:
                job.message = "окружение больше не разрешено для запуска; задача ожидает настройки"
                continue
            stand = next((s for s in project.stands if s.name == job.target), None) if job.kind == "stand" else next(
                (s for s in project.stands if job.target in s.environments), None)
            if job.kind not in ("stand-build", "stand-delete", "stand-control") and (
                    stand is not None and stand.source == "database" or
                    any(s.source == "database" and s.name == (job.spec or {}).get("stand") for s in project.stands)):
                job.message = "исполнение управляемого стенда ещё не подключено"
                continue
            if job.kind == "stand" and stand is None:
                job.message = "конфигурация стенда недоступна; задача ожидает настройки"
                continue
        if draining and job.parent_job_id is None:
            continue
        parent = active.get(job.parent_job_id) if job.parent_job_id is not None else None
        if job.parent_job_id is not None:
            project = projects.get(job.project)
            if (parent is None or parent.kind != "run" or parent.cancel_requested or parent.project != job.project
                    or not project or project.stand_of(parent.target) != job.target
                    or not (job.spec or {}).get("data_operation")):
                job.status, job.finished = "cancelled", now()
                job.message = "родительский прогон завершён или отменён"
                continue
        if required_capability(project, job) not in data.capabilities:
            continue
        has_test_data = job.kind == "stand-build" and bool((job.spec.get("test_data") or {}).get("entries"))
        if has_test_data and DATA_CAPABILITY not in data.capabilities:
            continue
        # Стенд занят прогоном или пересборкой: задача ждёт. Порядок внутри стенда сохраняется - более поздние
        # занимающие задачи не обгоняют ждущую.
        # Дочерняя операция обходит обычные queued задачи, ожидающие её родителя,
        # но НЕ другой running child или другую независимую блокировку стенда.
        workspace_jobs = {"stand-build", "stand-delete", "stand-control"}
        busy = any(key(other) == key(job) and other.id != job.parent_job_id and (
            (locks(job) and locks(other)) or (job.kind in workspace_jobs and other.kind in workspace_jobs)
        ) for other in running)
        if busy or (locks(job) and job.parent_job_id is None and key(job) in waiting):
            if locks(job):
                waiting.add(key(job))
            if job.kind in ("stand-build", "stand-delete", "stand-control"):
                job.message = "Ожидает освобождения стенда"
            continue
        test_data = []
        if has_test_data:
            from .stand_test_data import seed_payload

            try:
                test_data = seed_payload(session, job)
            except HTTPException as error:
                job.status, job.finished, job.message = "failed", now(), str(error.detail)
                instance.status = "failed"
                continue
        moment = now()
        job.status = "running"
        job.agent = principal.name
        job.started = moment
        job.heartbeat = moment
        job.message = None
        if job.kind == "run":
            # Что на стенде - на момент старта, а не постановки в очередь: между ними стенд могли пересобрать.
            product = product_on(stand)
            if run_target:
                job.spec = {**job.spec, "stand_target": {
                    **run_target, "build_id": stand.build_info["build_id"], "bindings": stand.output_bindings,
                }}
            if product:
                job.spec = {**job.spec, "product": product}
        session.commit()
        from .test_data import agent_operations

        private = agent_operations(session, job)
        spec = {**job.spec, "parent_job_id": job.parent_job_id}
        if job.kind == "stand-build":
            spec.pop("retry_request_key", None)
            spec.pop("retry_request_hash", None)
            spec.pop("test_data", None)
            if test_data:
                spec["test_data"] = test_data
        if private:
            spec["data_operations"] = private
        return ClaimOut(id=job.id, kind=job.kind, spec=spec)
    session.commit()
    response.status_code = 204
    return None


def _agent_job(session: Session, job_id: int, principal: Principal) -> Job:
    job = session.scalars(select(Job).where(Job.id == job_id).with_for_update()).first()
    if job is None or job.agent != principal.name or job.status != "running":
        raise HTTPException(409, "задача не выполняется этим агентом")
    return job


@router.post("/agent/jobs/{job_id}/log", response_model=LogOut)
def job_log(job_id: int, data: LogIn, principal: Principal = Depends(agents), session: Session = Depends(get_session)):
    job = _agent_job(session, job_id, principal)
    if job.kind == "stand-control":
        from .stand_control import require_control_agent

        require_control_agent(session, job, principal)
    job.heartbeat = now()
    if data.text and not (job.spec or {}).get("data_operation"):
        session.add(JobLog(job_id=job_id, text=data.text))
    if job.parent_job_id is not None:
        parent = session.get(Job, job.parent_job_id)
        if parent is None or parent.status != "running" or parent.cancel_requested:
            job.cancel_requested = True
    session.commit()
    return LogOut(cancel=job.cancel_requested)


@router.post("/agent/jobs/{job_id}/finish", response_model=JobOut)
def finish_job(job_id: int, data: FinishIn, principal: Principal = Depends(agents), session: Session = Depends(get_session)):
    _claim_lock(session)
    job = _agent_job(session, job_id, principal)
    if job.kind in ("stand-build", "stand-delete"):
        raise HTTPException(409, "операция стенда завершается через специализированный stand endpoint")
    if job.kind == "stand-control":
        from .stand_control import require_control_agent

        require_control_agent(session, job, principal)
    if data.status not in ("done", "failed", "cancelled"):
        raise HTTPException(422, "неизвестный итог")
    parent = session.get(Job, job.parent_job_id) if job.parent_job_id is not None else None
    cancelled = job.cancel_requested or (job.parent_job_id is not None and (
        parent is None or parent.status != "running" or parent.cancel_requested))
    job.status = "cancelled" if cancelled else data.status
    if job.status == "done":
        from .models import DataOperation

        operations = session.scalars(select(DataOperation).where(DataOperation.job_id == job.id)).all()
        # Старый агент не должен молча пропустить новые адаптеры и запустить smoke.
        if any(operation.result is None for operation in operations) or (
                (job.spec or {}).get("data_operation") and not operations):
            job.status = "failed"
            data.message = "не все операции с тестовыми данными завершены"
    job.run_id = data.run_id
    job.message = (None if job.status == "done" else "операция с данными " + job.status) if (
        job.spec or {}).get("data_operation") else data.message
    job.finished = now()
    _cancel_children(session, job.id)
    if job.status == "done":
        _smoke_after(session, job)
    session.commit()
    return job


@router.put("/agent/jobs/{job_id}/artifact/{run_id}/{name}", status_code=204)
async def upload_artifact(
    job_id: int, run_id: str, name: str, request: Request,
    principal: Principal = Depends(agents), session: Session = Depends(get_session),
):
    job = _agent_job(session, job_id, principal)
    if job.kind != "run":
        raise HTTPException(403, "артефакты доступны только прогонам")
    if name not in ARTIFACTS or not SAFE_RUN_ID.match(run_id):
        raise HTTPException(422, "недопустимый файл")
    body = await request.body()
    if len(body) > MAX_ARTIFACT:
        raise HTTPException(413, "файл слишком большой")
    target = artifacts_dir() / run_id / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    temporary.write_bytes(body)
    temporary.replace(target)
