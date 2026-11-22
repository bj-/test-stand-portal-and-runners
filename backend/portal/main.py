from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ai_guard, git_servers, help, jobs, managed_stands, stand_execution, stands, test_data, updates, users
from .auth import ADMIN, COOKIE, LLM, READERS, TESTER, USER_ROLES, Principal, require, require_read
from .config import artifacts_dir, static_dir
from .db import get_session
from .importer import sync_project
from .models import Bug, Run, TestResult
from .registry import RegistryError, load_registry

app = FastAPI(title="Test Portal", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RegistryError)
async def registry_error(request: Request, error: RegistryError):
    return JSONResponse(status_code=409, content={"detail": str(error)})


@app.middleware("http")
async def clear_invalid_session_cookie(request: Request, call_next):
    response = await call_next(request)
    # Dependency Response headers are not copied to FileResponse or error responses.
    if getattr(request.state, "clear_portal_session", False):
        response.delete_cookie(COOKIE)
    return response


# Предметный просмотр доступен гостю с ролью viewer; sync отдельно требует входа и роли.
# Токен llm получает отчёт без закрытых частей, Allure - нет
# (сырые запросы и ответы не отфильтровать): ADR 0002, п. 8.
api = APIRouter(prefix="/api", dependencies=[Depends(require_read(*READERS))])
people = Depends(require_read(*USER_ROLES))


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    title: str


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project: str
    suite: str
    kind: str | None
    modules: list
    started: datetime | None
    finished: datetime | None
    who: str | None
    trigger: str | None
    stand_name: str | None
    stand_url: str | None
    stand_kind: str | None
    build: str | None
    tests_branch: str | None
    tests_commit: str | None
    engine_version: str | None
    runner: str | None
    gate_failed: bool
    tests: int
    passed: int
    failed: int
    errors: int
    no_checks: int
    skipped: int
    checks: int
    checks_failed: int
    bug_ids: list


class ResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    position: int
    path: str
    name: str
    method: str | None
    url: str | None
    code: int | None
    time_ms: int | None
    verdict: str
    gate: bool
    groups: list


class RunDetail(RunOut):
    results: list[ResultOut]
    has_report: bool
    has_allure: bool


class RunPage(BaseModel):
    total: int
    items: list[RunOut]


class BugOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project: str
    number: int
    title: str
    module: str | None
    type: str | None
    severity: str | None
    priority: str | None
    status: str | None
    restricted: bool
    groups: list
    stand: str | None
    found_date: str | None
    run_id: str | None
    tracker: str | None


class BugDetail(BugOut):
    # restricted_body сюда не входит: ADR 0002, п. 8
    body: str


class Compare(BaseModel):
    base: str
    target: str
    new_failures: list[str]
    fixed: list[str]
    still_failing: list[str]
    added: list[str]
    removed: list[str]


class TrendPoint(BaseModel):
    id: str
    started: datetime | None
    suite: str
    passed: int
    failed: int
    other: int


class CountItem(BaseModel):
    name: str
    count: int


class FailingTest(BaseModel):
    path: str
    failed: int
    runs: int


class Stats(BaseModel):
    runs: int
    tests_executed: int
    pass_rate: float | None
    trend: list[TrendPoint]
    bugs_by_severity: list[CountItem]
    bugs_by_status: list[CountItem]
    top_failing: list[FailingTest]
    unstable: list[FailingTest]


def _run_or_404(session: Session, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "прогон не найден")
    return run


def _run_file(session: Session, run: Run, name: str) -> Path | None:
    """Файл прогона в клоне результатов. Пути берутся из индекса, но наружу не выходим в любом случае."""
    project = stands._project(session, run.project)
    if project.results is None:
        return None
    root = (project.results / run.path).resolve()
    file = (root / name).resolve()
    if not file.is_relative_to(project.results) or not file.is_file():
        return None
    return file


def _allure_file(session: Session, run: Run) -> Path | None:
    """Allure хранится вне git: прогоны портала лежат в артефактах, локальные - рядом с результатами, если есть."""
    uploaded = artifacts_dir() / run.id / "allure-report.html"
    return uploaded if uploaded.is_file() else _run_file(session, run, "allure-report.html")


def _main_stands(query):
    """Статистика - только по постоянным стендам: временные проверяют одну ветку (ADR 0002, п. 10)."""
    return query.where(Run.stand_kind.is_distinct_from("ephemeral"))


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@api.get("/openapi.json", include_in_schema=False)
def openapi() -> dict:
    return get_openapi(title=app.title, version=app.version, routes=app.routes)


@api.post("/sync", dependencies=[Depends(require(ADMIN, TESTER))])
def sync(project: str | None = None, session: Session = Depends(get_session)) -> dict:
    registry = load_registry(session)
    selected = [stands._project(session, project)] if project else registry.projects.values()
    result = {code: {"error": error} for code, error in registry.errors.items()} if not project else {}
    for item in selected:
        if item.results is None:
            result[item.code] = {"skipped": "репозиторий результатов не настроен"}
        else:
            result[item.code] = vars(sync_project(session, item))
    return result


@api.get("/projects", response_model=list[ProjectOut])
def projects(session: Session = Depends(get_session)):
    return load_registry(session).summaries


@api.get("/runs", response_model=RunPage)
def runs(
    project: str | None = None,
    suite: str | None = None,
    stand: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    query = select(Run)
    if project:
        query = query.where(Run.project == project)
    if suite:
        query = query.where(Run.suite == suite)
    if stand:
        query = query.where(Run.stand_name == stand)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    items = session.scalars(query.order_by(Run.started.desc().nulls_last()).limit(limit).offset(offset)).all()
    return RunPage(total=total or 0, items=items)


@api.get("/runs/{run_id}", response_model=RunDetail)
def run_detail(run_id: str, session: Session = Depends(get_session)):
    run = _run_or_404(session, run_id)
    detail = RunOut.model_validate(run).model_dump()
    return RunDetail(
        **detail,
        results=[ResultOut.model_validate(r) for r in run.results],
        has_report=_run_file(session, run, "report.md") is not None,
        has_allure=_allure_file(session, run) is not None,
    )


@api.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def run_report(run_id: str, principal: Principal = Depends(require_read(*READERS)), session: Session = Depends(get_session)):
    run = _run_or_404(session, run_id)
    if principal.role == LLM and ai_guard.in_restricted_dir(run.path):
        raise HTTPException(403, "прогон закрыт для ассистента: читает человек")
    file = _run_file(session, run, "report.md")
    if file is None:
        raise HTTPException(404, "отчёта нет")
    text = file.read_text(encoding="utf-8")
    # Ассистенту - как autotest.py view движка: блоки ai-restricted скрыты.
    return ai_guard.strip_blocks(text) if principal.role == LLM else text


@api.get("/runs/{run_id}/allure", dependencies=[people])
def run_allure(run_id: str, session: Session = Depends(get_session)):
    file = _allure_file(session, _run_or_404(session, run_id))
    if file is None:
        raise HTTPException(404, "отчёта Allure нет: он хранится вне git")
    return FileResponse(file, media_type="text/html")


@api.get("/runs/{base}/compare/{target}", response_model=Compare)
def compare(base: str, target: str, session: Session = Depends(get_session)):
    old = {r.path: r.verdict for r in _run_or_404(session, base).results}
    new = {r.path: r.verdict for r in _run_or_404(session, target).results}
    both = old.keys() & new.keys()
    return Compare(
        base=base,
        target=target,
        new_failures=sorted(p for p in both if new[p] == "failed" and old[p] != "failed"),
        fixed=sorted(p for p in both if old[p] == "failed" and new[p] != "failed"),
        still_failing=sorted(p for p in both if old[p] == "failed" and new[p] == "failed"),
        added=sorted(new.keys() - old.keys()),
        removed=sorted(old.keys() - new.keys()),
    )


@api.get("/bugs", response_model=list[BugOut])
def bugs(
    project: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    query = select(Bug)
    if project:
        query = query.where(Bug.project == project)
    if severity:
        query = query.where(Bug.severity == severity)
    if status:
        query = query.where(Bug.status == status)
    return session.scalars(query.order_by(Bug.number.desc())).all()


@api.get("/bugs/{bug_id}", response_model=BugDetail)
def bug_detail(bug_id: str, session: Session = Depends(get_session)):
    bug = session.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(404, "баг не найден")
    return bug


@api.get("/stats", response_model=Stats)
def stats(project: str | None = None, last: int = Query(30, ge=2, le=200), session: Session = Depends(get_session)):
    query = _main_stands(select(Run))
    if project:
        query = query.where(Run.project == project)
    recent = session.scalars(query.order_by(Run.started.desc().nulls_last()).limit(last)).all()
    recent.reverse()

    history: dict[str, list[str]] = defaultdict(list)
    for run in recent:
        for result in run.results:
            history[result.path].append(result.verdict)

    def failing(path: str, verdicts: list[str]) -> FailingTest:
        return FailingTest(path=path, failed=verdicts.count("failed"), runs=len(verdicts))

    top = sorted((failing(p, v) for p, v in history.items() if "failed" in v), key=lambda t: -t.failed)[:15]
    unstable = [
        failing(path, verdicts)
        for path, verdicts in history.items()
        if sum(a != b for a, b in zip(verdicts, verdicts[1:])) >= 2
    ]
    unstable.sort(key=lambda t: -t.runs)

    bug_query = select(Bug)
    if project:
        bug_query = bug_query.where(Bug.project == project)
    all_bugs = session.scalars(bug_query).all()
    passed = sum(r.passed for r in recent)
    executed = sum(r.passed + r.failed + r.errors for r in recent)
    count = lambda values: [CountItem(name=k or "-", count=v) for k, v in Counter(values).most_common()]  # noqa: E731
    return Stats(
        runs=len(recent),
        tests_executed=executed,
        pass_rate=round(passed / executed, 4) if executed else None,
        trend=[
            TrendPoint(
                id=r.id, started=r.started, suite=r.suite, passed=r.passed, failed=r.failed,
                other=r.errors + r.no_checks + r.skipped,
            )
            for r in recent
        ],
        bugs_by_severity=count(b.severity for b in all_bugs),
        bugs_by_status=count(b.status for b in all_bugs),
        top_failing=top,
        unstable=unstable[:15],
    )


app.include_router(users.router)
app.include_router(updates.router)
app.include_router(jobs.router)
app.include_router(managed_stands.router)
app.include_router(git_servers.router)
app.include_router(stand_execution.router)
app.include_router(test_data.router)
app.include_router(stands.router)
app.include_router(help.router)
app.include_router(api)


class SpaFiles(StaticFiles):
    """Прямая ссылка на страницу приложения отдаёт index.html; маршруты решает React."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except Exception:
            if path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


if static_dir().is_dir():
    app.mount("/", SpaFiles(directory=static_dir(), html=True), name="ui")
