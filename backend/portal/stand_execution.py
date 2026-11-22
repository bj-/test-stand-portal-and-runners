"""Host-bound portable stand execution. Frozen inputs never pass through public projections."""
import copy
import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .auth import AGENT, DEVELOPER, USER_ROLES, Principal, cookie_user, now, require
from .config import artifacts_dir
from .db import get_session
from .jobs import JobOut, TERMINAL_STATES, _agent_job, _claim_lock
from .managed_stands import Input, _commit, _mutation_lock, creators
from .models import ApiToken, Job, StandBuild, StandInstance, StandPortReservation, StandTemplate, User
from .stand_contract import LIVENESS_STAGE, STAGES, StandError, binding_titles, canonical, template_hash, validate_snapshot
from .stands import _project, lock_key, locks
from .test_data import DataRoute

router = APIRouter(prefix="/api", route_class=DataRoute)
agents = require(AGENT)
readers = require(*USER_ROLES)
ARTIFACTS = {"allure-report.html": 64 * 1024 * 1024, "results.json": 8 * 1024 * 1024,
             "junit.xml": 8 * 1024 * 1024}
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
EXECUTION_STAGES = (*STAGES, LIVENESS_STAGE)
STAGE_NAME = Literal["build", "deploy", "migrate", "seed", "healthcheck", "smoke", "liveness"]


class Payload(Input):
    @model_validator(mode="after")
    def bounded(self):
        if len(canonical(self.model_dump(mode="json"))) > 2 * 1024 * 1024:
            raise ValueError("JSON превышает 2 MiB")
        return self


class SnapshotIn(Payload):
    snapshot: dict


class ProgressIn(Payload):
    stage: Literal["prepare", STAGE_NAME]
    status: Literal["running", "success", "failed"]


class StageIn(Payload):
    name: STAGE_NAME
    status: Literal["running", "success", "failed", "cancelled", "skipped"]
    started_at: str | None = Field(default=None, max_length=64)
    finished_at: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def timestamps(self):
        try:
            first = datetime.fromisoformat(self.started_at) if self.started_at else None
            last = datetime.fromisoformat(self.finished_at) if self.finished_at else None
            if first and last and last < first:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("неверное время этапа") from None
        return self


class ImageIn(Payload):
    id: str = Field(pattern=IMAGE_ID)
    repo_digests: list[str] = Field(max_length=128)
    repo_tags: list[str] = Field(max_length=128)


class FinishIn(Payload):
    status: Literal["done", "failed", "cancelled"]
    stages: list[StageIn] = Field(default_factory=list, max_length=len(EXECUTION_STAGES))
    images: list[ImageIn] = Field(default_factory=list, max_length=128)
    output_bindings: dict[str, str] = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=2000)


class RetryIn(Payload):
    build_id: str = Field(min_length=1, max_length=128)


class DeleteFinishIn(Payload):
    status: Literal["done", "failed"]
    message: str | None = Field(default=None, max_length=2000)


def bound_agent(session, principal):
    return session.scalar(select(ApiToken).where(ApiToken.name == principal.name,
        ApiToken.role == AGENT, ApiToken.active.is_(True), ApiToken.stand_host.is_not(None)))


def _job(session, job_id, principal):
    job = _agent_job(session, job_id, principal)
    token = bound_agent(session, principal)
    if job.kind != "stand-build" or token is None or token.stand_host != job.spec.get("target_host"):
        raise HTTPException(403, "агент не привязан к хосту этой сборки")
    instance = session.get(StandInstance, job.target)
    if instance is None or instance.project != job.project or instance.target_host != token.stand_host:
        raise HTTPException(409, "экземпляр сборки недоступен")
    return job, instance


def _build(session, job, required=True):
    build = session.get(StandBuild, "build-" + str(job.id))
    if build and (build.project != job.project or build.stand_id != job.target or build.job_id != job.id):
        raise HTTPException(409, "несогласованная сборка")
    if required and build is None:
        raise HTTPException(409, "сначала сохраните snapshot")
    return build


def _validate_frozen(snapshot, spec):
    try:
        repositories = {}
        for component in snapshot.get("components", []):
            repositories.setdefault(component["repository"], {"components": []})["components"].append(component["id"])
        validate_snapshot(snapshot, repositories)
        request = spec["request"]
        actual = {c["id"]: {"repository": c["repository"], "ref": c["requested_ref"]} for c in snapshot["components"]}
        if (canonical(snapshot["template"]) != canonical(spec["template"])
                or snapshot["template_sha256"] != spec["template_sha256"]
                or snapshot["template_sha256"] != template_hash(spec["template"])
                or snapshot["profile"] != request["profile"]
                or canonical(snapshot["parameters"]) != canonical(request["parameters"])
                or actual != request["components"]
                or ("snapshot" in spec and canonical(snapshot) != canonical(spec["snapshot"]))):
            raise ValueError()
    except (StandError, ValueError, TypeError, KeyError, RecursionError):
        raise HTTPException(422, "snapshot не соответствует замороженному запросу portable v1") from None


@router.post("/agent/jobs/{job_id}/stand/snapshot")
def snapshot(job_id: int, body: SnapshotIn, principal: Principal = Depends(agents), session: Session = Depends(get_session)):
    job, instance = _job(session, job_id, principal)
    existing = _build(session, job, required=False)
    if existing:
        if canonical(existing.snapshot) != canonical(body.snapshot):
            raise HTTPException(409, "snapshot сборки неизменяем")
        job.heartbeat = now()
        session.commit()
        return {"build_id": existing.id}
    _validate_frozen(body.snapshot, job.spec)
    build = StandBuild(id="build-" + str(job.id), project=job.project, stand_id=instance.id,
        template_key=instance.template_key, template_version=instance.template_version,
        snapshot=copy.deepcopy(body.snapshot), job_id=job.id, status="running", started=now(),
        result={"stages": []})
    session.add(build)
    instance.status = "building"
    job.heartbeat = now()
    session.commit()
    return {"build_id": build.id}


@router.post("/agent/jobs/{job_id}/stand/progress")
def progress(job_id: int, body: ProgressIn, principal: Principal = Depends(agents), session: Session = Depends(get_session)):
    job, instance = _job(session, job_id, principal)
    build = _build(session, job, required=body.stage != "prepare")
    if body.stage == "prepare" and build and (build.result or {}).get("stages"):
        raise HTTPException(409, "подготовка уже завершена")
    if body.stage != "prepare":
        stages = copy.deepcopy((build.result or {}).get("stages", []))
        position = EXECUTION_STAGES.index(body.stage)
        previous_statuses = [s["status"] for s in stages[:position]]
        if body.stage == LIVENESS_STAGE:
            if not _can_check_liveness(build, previous_statuses):
                raise HTTPException(409, "отклик проверяется после сбоя smoke при наличии проверок в профиле")
        elif position > len(stages) or any(status != "success" for status in previous_statuses):
            raise HTTPException(409, "этапы должны выполняться последовательно")
        if position < len(stages):
            previous = stages[position]["status"]
            if previous != "running" and previous != body.status:
                raise HTTPException(409, "завершённый этап неизменяем")
            if position != len(stages) - 1 and previous != body.status:
                raise HTTPException(409, "предыдущий этап уже завершён")
            stages[position] = {"name": body.stage, "status": body.status}
        else:
            stages.append({"name": body.stage, "status": body.status})
        build.result = {**(build.result or {}), "stages": stages}
        instance.status = ("building" if body.stage == "build" else "deploying"
                           if body.stage in ("deploy", "migrate", "seed") else "checking")
    job.heartbeat = now()
    session.commit()
    return {"cancel": job.cancel_requested}


def _artifact_path(build_id, name):
    if name not in ARTIFACTS or not re.fullmatch(r"build-[0-9]+", build_id):
        raise HTTPException(422, "недопустимый артефакт")
    root = artifacts_dir().resolve()
    destination = root / "stand-builds" / build_id / name
    if not destination.resolve().is_relative_to(root):
        raise HTTPException(409, "недопустимое расположение артефакта")
    return destination


@router.put("/agent/jobs/{job_id}/stand/artifacts/{name}", status_code=204)
async def upload(job_id: int, name: str, request: Request, principal: Principal = Depends(agents),
                 session: Session = Depends(get_session)):
    job, _ = _job(session, job_id, principal)
    build = _build(session, job)
    destination = _artifact_path(build.id, name)
    cap = ARTIFACTS[name]
    if request.headers.get("content-length"):
        try:
            if int(request.headers["content-length"]) > cap:
                raise HTTPException(413, "артефакт превышает допустимый размер")
        except ValueError:
            raise HTTPException(422, "неверная длина артефакта") from None
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > cap:
                    raise HTTPException(413, "артефакт превышает допустимый размер")
                stream.write(chunk)
        os.replace(temporary, destination)
        build.artifact_refs = sorted(set(build.artifact_refs or []) | {name})
        job.heartbeat = now()
        session.commit()
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_out(build, titles=None):
    snapshot = build.snapshot
    result = build.result or {}
    return {"id": build.id, "project": build.project, "stand_id": build.stand_id, "job_id": build.job_id,
        "status": build.status, "created": build.created.replace(tzinfo=UTC),
        "started": build.started.replace(tzinfo=UTC) if build.started else None,
        "finished": build.finished.replace(tzinfo=UTC) if build.finished else None,
        "template_key": build.template_key, "template_version": build.template_version,
        "profile": snapshot.get("profile"),
        "components": [{"id": c["id"], "repository": c["repository"], "requested_ref": c["requested_ref"], "sha": c["sha"]}
                       for c in snapshot.get("components", [])],
        "stages": result.get("stages", []), "images": build.images,
        "output_bindings": result.get("output_bindings", {}),
        "output_binding_titles": binding_titles(snapshot.get("template", {}), titles),
        "artifacts": [{"name": name, "url": f"/api/stand-builds/{build.id}/artifacts/{name}"}
                      for name in build.artifact_refs or [] if name in ARTIFACTS]}


@router.get("/stand-builds/{build_id}")
def get_build(build_id: str, _=Depends(readers), session: Session = Depends(get_session)):
    build = session.get(StandBuild, build_id)
    if build is None:
        raise HTTPException(404, "сборка не найдена")
    template = session.get(StandTemplate, (build.project, build.template_key, build.template_version))
    return build_out(build, template.output_binding_titles if template else None)


@router.get("/projects/{code}/stand-instances/{instance_id}/builds")
def builds(code: str, instance_id: str, _=Depends(readers), session: Session = Depends(get_session)):
    row = session.get(StandInstance, instance_id)
    if row is None or row.project != code:
        raise HTTPException(404, "стенд не найден")
    template = session.get(StandTemplate, (code, row.template_key, row.template_version))
    return [build_out(build, template.output_binding_titles if template else None) for build in session.scalars(select(StandBuild).where(
        StandBuild.project == code, StandBuild.stand_id == instance_id).order_by(StandBuild.created.desc(), StandBuild.id.desc()))]


@router.get("/stand-builds/{build_id}/artifacts/{name}")
def artifact(build_id: str, name: str, _=Depends(readers), session: Session = Depends(get_session)):
    build = session.get(StandBuild, build_id)
    if build is None or name not in (build.artifact_refs or []):
        raise HTTPException(404, "артефакт не найден")
    target = _artifact_path(build.id, name)
    if not target.is_file():
        raise HTTPException(404, "артефакт не найден")
    return FileResponse(target, filename=name,
                        media_type="text/html" if name == "allure-report.html" else None,
                        content_disposition_type="inline" if name == "allure-report.html" else "attachment",
                        headers={"X-Content-Type-Options": "nosniff"})


def _can_check_liveness(build, previous_statuses):
    if build is None or previous_statuses != ["success"] * (len(STAGES) - 1) + ["failed"]:
        return False
    template = build.snapshot["template"]
    selected = template["profiles"][build.snapshot["profile"]]["services"]
    return any(service in template.get(LIVENESS_STAGE, {}) for service in selected)


def _validate_completion(body, job, build):
    names = [stage.name for stage in body.stages]
    if names != list(EXECUTION_STAGES[:len(names)]):
        raise HTTPException(422, "неверный порядок этапов")
    reported = {stage.name: stage.status for stage in body.stages}
    for stage in (build.result or {}).get("stages", []) if build else []:
        if stage["status"] in ("success", "failed") and reported.get(stage["name"]) != stage["status"]:
            raise HTTPException(409, "итог противоречит завершённым этапам")
    statuses = [stage.status for stage in body.stages]
    has_liveness = len(names) == len(EXECUTION_STAGES)
    if has_liveness and not _can_check_liveness(build, statuses[:-1]):
        raise HTTPException(422, "отклик проверяется после сбоя smoke при наличии проверок в профиле")
    if body.status != "done" or job.cancel_requested:
        return False
    limited = has_liveness and statuses[-1] == "success"
    full = names == list(STAGES) and all(status == "success" for status in statuses)
    if build is None or not body.images or not (full or limited):
        raise HTTPException(422, "для готовности нужны snapshot, образы и пройденный smoke или подтверждённый отклик")
    if full and "allure-report.html" not in (build.artifact_refs or []):
        raise HTTPException(422, "для успешного smoke нужен отчёт Allure")
    publish_ip = job.spec.get("publish_ip", "127.0.0.1")
    expected = {name: f"{binding['scheme']}://{publish_ip}:{job.spec['ports'][name]}{binding['path']}"
                for name, binding in job.spec["template"]["output_bindings"].items()}
    if body.output_bindings != expected:
        raise HTTPException(422, "ссылки не соответствуют зарезервированным портам шаблона")
    return limited


@router.post("/agent/jobs/{job_id}/stand/finish", response_model=JobOut)
def finish(job_id: int, body: FinishIn, principal: Principal = Depends(agents), session: Session = Depends(get_session)):
    _claim_lock(session)
    job, instance = _job(session, job_id, principal)
    build = _build(session, job, required=False)
    limited = _validate_completion(body, job, build)
    job.status = "cancelled" if job.cancel_requested else body.status
    job.finished, job.heartbeat = now(), now()
    job.message = "Smoke не пройден. Отклик стенда подтверждён." if limited else body.message
    if build:
        build.status, build.finished = job.status, job.finished
        build.result = {"stages": [stage.model_dump(exclude_none=True) for stage in body.stages],
                        "output_bindings": dict(body.output_bindings) if job.status == "done" else {}}
        build.images = [item.model_dump() for item in body.images]
    if job.status == "done":
        instance.status = "limited" if limited else "ready"
        instance.deployed_build_id = build.id
    else:
        instance.status = "failed"
    session.commit()
    return job


@router.post("/agent/jobs/{job_id}/stand/delete-finish", response_model=JobOut)
def delete_finish(job_id: int, body: DeleteFinishIn, principal: Principal = Depends(agents),
                  session: Session = Depends(get_session)):
    _mutation_lock(session)
    _claim_lock(session)
    job = _agent_job(session, job_id, principal)
    token = bound_agent(session, principal)
    if job.kind != "stand-delete" or token is None or token.stand_host != job.spec.get("target_host"):
        raise HTTPException(403, "агент не привязан к хосту удаления")
    instance = session.get(StandInstance, job.target)
    if (instance is None or instance.project != job.project or instance.target_host != token.stand_host
            or instance.deleted_at is not None):
        raise HTTPException(409, "экземпляр удаления недоступен")
    # A successful cleanup is authoritative even when cancellation was requested during it.
    # Failure, cancellation and lost agents never release a reservation.
    job.status, job.finished, job.heartbeat, job.message = body.status, now(), now(), body.message
    if body.status == "done":
        instance.deleted_at = job.finished
        session.execute(delete(StandPortReservation).where(StandPortReservation.stand_id == instance.id))
    session.commit()
    return job


@router.post("/projects/{code}/stand-instances/{instance_id}/retry", response_model=JobOut, status_code=201)
def retry(code: str, instance_id: str, body: RetryIn, response: Response,
          idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
          principal: Principal = Depends(creators), owner: User = Depends(cookie_user),
          session: Session = Depends(get_session)):
    if not idempotency_key.strip() or not idempotency_key.isprintable():
        raise HTTPException(422, "неверный ключ идемпотентности")
    digest = hashlib.sha256(canonical({"body": body.model_dump(mode="json"), "owner": owner.login,
                                      "project": code, "stand": instance_id})).hexdigest()
    _mutation_lock(session)
    _claim_lock(session)
    try:
        instance = available_instance(session, code, instance_id)
        require_owner(instance, principal, owner)
        previous_jobs = session.scalars(select(Job).where(Job.project == code, Job.kind == "stand-build")).all()
        for previous in previous_jobs:
            if previous.spec.get("retry_request_key") == idempotency_key:
                if previous.spec.get("retry_request_hash") != digest:
                    raise HTTPException(409, "ключ идемпотентности уже использован")
                response.status_code = 200
                session.rollback()
                return previous
        project = _project(session, code)
        job = queue_rebuild(session, project, instance, body.build_id, owner.login,
                            retry_request_key=idempotency_key, retry_request_hash=digest)
        _commit(session)
        return job
    except Exception:
        session.rollback()
        raise


def available_instance(session, code, instance_id):
    """Called while holding mutation then claim locks for every managed mutation."""
    instance = session.get(StandInstance, instance_id)
    if instance is None or instance.project != code:
        raise HTTPException(404, "стенд не найден")
    if instance.deleted_at is not None:
        raise HTTPException(409, "стенд удалён")
    if session.scalar(select(Job.id).where(Job.project == code, Job.target == instance_id,
            Job.kind == "stand-delete", Job.status.in_(("queued", "running"))).limit(1)) is not None:
        raise HTTPException(409, "стенд ожидает или выполняет удаление")
    return instance


def require_owner(instance, principal, owner):
    if principal.role == DEVELOPER and instance.owner_login != owner.login:
        raise HTTPException(403, "разработчик управляет только своими стендами")


def queue_rebuild(session, project, instance, build_id, requested_by, **request_metadata):
    """Retry and the deploy action share immutable inputs, data plan and queue checks."""
    build = session.get(StandBuild, build_id)
    if build is None or build.project != project.code or build.stand_id != instance.id:
        raise HTTPException(422, "сборка не принадлежит стенду")
    original = session.get(Job, build.job_id)
    if (original is None or original.project != project.code or original.kind != "stand-build"
            or original.target != instance.id or original.status not in TERMINAL_STATES):
        raise HTTPException(409, "исходная сборка ещё не завершена")
    if (original.spec.get("stand") != instance.id or original.spec.get("project") != project.code
            or original.spec.get("target_host") != instance.target_host):
        raise HTTPException(409, "исходная сборка не соответствует экземпляру стенда")
    pending = session.scalars(select(Job).where(Job.project == project.code, Job.status.in_(("queued", "running"))))
    if any(locks(job) and lock_key(project, job) == f"{project.code}:stand:{instance.id}" for job in pending):
        raise HTTPException(409, "стенд уже ожидает или выполняет сборку")
    _validate_frozen(build.snapshot, original.spec)
    spec = copy.deepcopy(original.spec)
    spec.pop("retry_request_key", None)
    spec.pop("retry_request_hash", None)
    spec.update(snapshot=copy.deepcopy(build.snapshot), **request_metadata)
    job = Job(project=project.code, kind="stand-build", status="queued", target=instance.id,
              title="Повторить сборку: " + instance.name, created_by=requested_by, created=now(), spec=spec,
              message="Ожидает исполнителя шаблонов stand-template-v1")
    session.add(job)
    instance.status = "waiting"
    return job
