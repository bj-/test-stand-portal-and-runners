"""Закрытый каталог данных и очередь доверенных адаптеров установки."""
import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import ADMIN, AGENT, DEVELOPER, TESTER, Principal, now, require
from .db import get_session
from .models import DataBinding, DataOperation, Dataset, DatasetVersion, Job
from .stands import _project, _stand, ensure_project, require_executor

class DataRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_validation(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                # Стандартный handler эхо-передаёт input, включая NaN: его JSON response сам падает.
                # Закрытый payload и объекты ctx не нужны для объяснения ошибки клиенту.
                raise HTTPException(422, [{key: item[key] for key in ("loc", "msg", "type") if key in item}
                                          for item in error.errors()]) from None

        return safe_validation


router = APIRouter(prefix="/api", route_class=DataRoute)
readers = require(ADMIN, TESTER, DEVELOPER)
managers = require(ADMIN, TESTER)
MAX_DATA = 2 * 1024 * 1024
FORMAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def validate_json(value: dict) -> dict:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HTTPException(422, "нужен JSON object без NaN/Infinity") from None
    if len(encoded) > MAX_DATA:
        raise HTTPException(422, "JSON превышает 2 MiB в UTF-8")
    return value


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionIn(Input):
    data: dict

    @field_validator("data")
    @classmethod
    def json_payload(cls, value):
        return validate_json(value)


class DatasetIn(VersionIn):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    format: str = Field(pattern=FORMAT.pattern)


class VersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    created_by: str
    created: datetime

    @field_validator("created")
    @classmethod
    def utc(cls, value):
        return value.replace(tzinfo=UTC)


class VersionOut(VersionSummary):
    data: dict


class DatasetSummary(VersionSummary):
    id: int
    key: str
    title: str
    description: str
    format: str
    can_edit: bool


class DatasetOut(DatasetSummary):
    versions: list[VersionSummary]


class BindingIn(Input):
    dataset_id: int = Field(gt=0)
    version: int = Field(gt=0)
    target: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    auto_apply: bool = True
    position: int = Field(default=0, ge=0, le=1000000)


class BindingOut(BindingIn):
    id: int
    title: str
    key: str
    format: str


class TargetOut(BaseModel):
    name: str
    title: str
    format: str
    can_apply: bool
    can_inspect: bool


class StandDataOut(BaseModel):
    targets: list[TargetOut]
    bindings: list[BindingOut]
    can_manage: bool


class ApplyIn(Input):
    dataset_id: int = Field(gt=0)
    version: int = Field(gt=0)
    target: str = Field(min_length=1, max_length=120)
    parent_job_id: int | None = Field(default=None, gt=0)


class InspectIn(Input):
    target: str = Field(min_length=1, max_length=120)
    query: dict
    parent_job_id: int | None = Field(default=None, gt=0)

    @field_validator("query")
    @classmethod
    def json_payload(cls, value):
        return validate_json(value)


class DataJobOut(BaseModel):
    id: int
    status: str
    message: str | None
    result: dict | None


class QueuedOut(BaseModel):
    id: int


def _dataset(session, code, dataset_id, *, lock=False):
    query = select(Dataset).where(Dataset.id == dataset_id, Dataset.project == code)
    if lock:
        query = query.with_for_update()
    dataset = session.scalars(query).first()
    if dataset is None:
        raise HTTPException(404, "набор не найден")
    return dataset


def _version(session, dataset, version):
    item = session.get(DatasetVersion, (dataset.id, version))
    if item is None:
        raise HTTPException(404, "версия набора не найдена")
    return item


def _summary(dataset, principal):
    fields = ("id", "key", "title", "description", "format", "version", "created_by", "created")
    return {**{name: getattr(dataset, name) for name in fields},
            "can_edit": principal.role in (ADMIN, TESTER) or dataset.created_by == principal.name}


def _commit(session, detail):
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, detail) from None


@router.get("/projects/{code}/datasets", response_model=list[DatasetSummary])
def datasets(code: str, created_by: str | None = None, principal: Principal = Depends(readers),
             session: Session = Depends(get_session)):
    _project(session, code)
    query = select(Dataset).where(Dataset.project == code)
    if created_by is not None:
        query = query.where(Dataset.created_by == created_by)
    return [_summary(d, principal) for d in session.scalars(query.order_by(Dataset.id.desc()))]


@router.post("/projects/{code}/datasets", response_model=DatasetSummary, status_code=201)
def create_dataset(code: str, data: DatasetIn, principal: Principal = Depends(readers),
                   session: Session = Depends(get_session)):
    project = _project(session, code)
    ensure_project(session, project)
    dataset = Dataset(project=code, **data.model_dump(exclude={"data"}), version=1,
                      created_by=principal.name, created=now())
    session.add(dataset)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "key уже используется в проекте") from None
    session.add(DatasetVersion(dataset_id=dataset.id, version=1, data=data.data,
                               created_by=principal.name, created=now()))
    _commit(session, "key уже используется в проекте")
    return _summary(dataset, principal)


@router.get("/projects/{code}/datasets/{dataset_id}", response_model=DatasetOut)
def dataset_detail(code: str, dataset_id: int, principal: Principal = Depends(readers),
                   session: Session = Depends(get_session)):
    _project(session, code)
    dataset = _dataset(session, code, dataset_id)
    versions = session.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)
                               .order_by(DatasetVersion.version.desc())).all()
    return {**_summary(dataset, principal), "versions": versions}


@router.post("/projects/{code}/datasets/{dataset_id}/versions", response_model=VersionSummary, status_code=201)
def create_version(code: str, dataset_id: int, data: VersionIn, principal: Principal = Depends(readers),
                   session: Session = Depends(get_session)):
    _project(session, code)
    # PostgreSQL: сериализуем публикации одного набора, версия и указатель обновляются одним commit.
    dataset = _dataset(session, code, dataset_id, lock=True)
    if principal.role == DEVELOPER and dataset.created_by != principal.name:
        raise HTTPException(403, "разработчик изменяет только свои наборы")
    dataset.version += 1
    version = DatasetVersion(dataset_id=dataset.id, version=dataset.version, data=data.data,
                             created_by=principal.name, created=now())
    session.add(version)
    _commit(session, "версия уже опубликована; повторите запрос")
    return version


@router.get("/projects/{code}/datasets/{dataset_id}/versions/{version}", response_model=VersionOut)
def version_detail(code: str, dataset_id: int, version: int, principal: Principal = Depends(readers),
                   session: Session = Depends(get_session)):
    _project(session, code)
    return _version(session, _dataset(session, code, dataset_id), version)


def json_response(value):
    return Response(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                    media_type="application/json")


@router.get("/projects/{code}/datasets/{dataset_id}/versions/{version}/content", response_model=dict)
def version_content(code: str, dataset_id: int, version: int, principal: Principal = Depends(readers),
                    session: Session = Depends(get_session)):
    _project(session, code)
    return json_response(_version(session, _dataset(session, code, dataset_id), version).data)


def _target(stand, target, operation=None):
    config = stand.test_data.get(target)
    if not isinstance(config, dict) or not FORMAT.fullmatch(str(config.get("format", ""))):
        raise HTTPException(422, "цель данных не настроена")
    if operation and (not isinstance(config.get(operation), str) or not config[operation].strip()):
        raise HTTPException(422, "операция недоступна для цели")
    if operation and not stand.workdir:
        raise HTTPException(422, "у стенда не задан workdir")
    return config


def _binding_out(session, binding):
    dataset = _dataset(session, binding.project, binding.dataset_id)
    return {**{name: getattr(binding, name) for name in BindingIn.model_fields}, "id": binding.id,
            "title": dataset.title, "key": dataset.key, "format": dataset.format}


def _validate_binding(session, code, stand, data):
    config = _target(stand, data.target, "apply")
    dataset = _dataset(session, code, data.dataset_id)
    _version(session, dataset, data.version)
    if dataset.format != config["format"]:
        raise HTTPException(422, "формат набора несовместим с целью")


@router.get("/projects/{code}/stands/{name}/test-data", response_model=StandDataOut)
def stand_data(code: str, name: str, principal: Principal = Depends(readers), session: Session = Depends(get_session)):
    stand = _stand(_project(session, code), name)
    targets = [TargetOut(name=key, title=str(cfg.get("title") or key), format=cfg["format"],
                         can_apply=bool(cfg.get("apply")), can_inspect=bool(cfg.get("inspect")))
               for key in stand.test_data for cfg in [_target(stand, key)]]
    bindings = session.scalars(select(DataBinding).where(DataBinding.project == code, DataBinding.stand == name)
                               .order_by(DataBinding.position, DataBinding.id)).all()
    return StandDataOut(targets=targets, bindings=[_binding_out(session, b) for b in bindings],
                        can_manage=principal.role in (ADMIN, TESTER))


@router.post("/projects/{code}/stands/{name}/test-data/bindings", response_model=BindingOut, status_code=201)
def create_binding(code: str, name: str, data: BindingIn, principal: Principal = Depends(managers),
                   session: Session = Depends(get_session)):
    stand = _stand(_project(session, code), name)
    _validate_binding(session, code, stand, data)
    binding = DataBinding(project=code, stand=name, **data.model_dump())
    session.add(binding)
    _commit(session, "набор уже привязан к этой цели стенда")
    return _binding_out(session, binding)


def _binding(session, code, name, binding_id):
    binding = session.get(DataBinding, binding_id)
    if binding is None or binding.project != code or binding.stand != name:
        raise HTTPException(404, "привязка не найдена")
    return binding


@router.put("/projects/{code}/stands/{name}/test-data/bindings/{binding_id}", response_model=BindingOut)
def update_binding(code: str, name: str, binding_id: int, data: BindingIn, principal: Principal = Depends(managers),
                   session: Session = Depends(get_session)):
    stand = _stand(_project(session, code), name)
    _validate_binding(session, code, stand, data)
    binding = _binding(session, code, name, binding_id)
    for key, value in data.model_dump().items():
        setattr(binding, key, value)
    _commit(session, "набор уже привязан к этой цели стенда")
    return _binding_out(session, binding)


@router.delete("/projects/{code}/stands/{name}/test-data/bindings/{binding_id}", status_code=204)
def delete_binding(code: str, name: str, binding_id: int, principal: Principal = Depends(managers),
                   session: Session = Depends(get_session)):
    _stand(_project(session, code), name)
    session.delete(_binding(session, code, name, binding_id))
    session.commit()


def operation_snapshot(session, code, stand, operation, target, dataset_id=None, version=None, query=None):
    require_executor(stand)
    config = _target(stand, target, operation)
    dataset = _dataset(session, code, dataset_id) if dataset_id is not None else None
    payload = _version(session, dataset, version).data if dataset else None
    if dataset and dataset.format != config["format"]:
        raise HTTPException(422, "формат набора несовместим с целью")
    try:
        timeout = int(config.get("timeout", 120))
    except (TypeError, ValueError):
        raise HTTPException(422, "неверный timeout адаптера") from None
    if not 1 <= timeout <= 3600:
        raise HTTPException(422, "timeout адаптера должен быть от 1 до 3600 секунд")
    return {
        "request": {"protocol": 1, "operation": operation, "project": code, "stand": stand.name,
                    "target": target, "format": config["format"], "data": payload, "query": query,
                    "dataset": {"id": dataset.id, "version": version} if dataset else None},
        "adapter": {"command": config[operation], "workdir": stand.workdir, "timeout": timeout},
    }


def snapshot_bindings(session, job, stand):
    """Версии, данные и доверенная команда фиксируются при постановке исходной задачи."""
    bindings = session.scalars(select(DataBinding).where(DataBinding.project == job.project,
        DataBinding.stand == stand.name, DataBinding.enabled.is_(True), DataBinding.auto_apply.is_(True))
        .order_by(DataBinding.position, DataBinding.id)).all()
    snapshots = [operation_snapshot(session, job.project, stand, "apply", b.target, b.dataset_id, b.version)
                 for b in bindings]
    session.flush()
    for position, snapshot in enumerate(snapshots):
        session.add(DataOperation(job_id=job.id, position=position, **snapshot))


def _queue_operation(session, code, name, principal, data, operation):
    project = _project(session, code)
    stand = _stand(project, name)
    require_executor(stand)
    parent_id = data.parent_job_id
    if parent_id is not None:
        # Блокируем родителя до commit: finish/cancel не могут пройти между проверкой и enqueue.
        parent = session.scalars(select(Job).where(Job.id == parent_id).with_for_update()).first()
        if (parent is None or parent.project != code or parent.kind != "run" or parent.status != "running"
                or parent.cancel_requested or project.stand_of(parent.target) != name):
            raise HTTPException(409, "нужен активный прогон этого стенда")
        if principal.role == DEVELOPER and parent.created_by != principal.name:
            raise HTTPException(403, "разработчик обслуживает только свой прогон")
    snapshot = operation_snapshot(session, code, stand, operation, data.target,
        getattr(data, "dataset_id", None), getattr(data, "version", None), getattr(data, "query", None))
    ensure_project(session, project)
    job = Job(project=code, kind="stand", target=name, title="Операция с тестовыми данными",
              parent_job_id=parent_id, status="queued", created_by=principal.name, created=now(),
              cancel_requested=False, spec={"project": code, "stand": name, "action": "test-data",
                                           "lock": True, "data_operation": True})
    session.add(job)
    session.flush()
    session.add(DataOperation(job_id=job.id, position=0, **snapshot))
    session.commit()
    return {"id": job.id}


@router.post("/projects/{code}/stands/{name}/test-data/apply", response_model=QueuedOut, status_code=201)
def apply_data(code: str, name: str, data: ApplyIn, principal: Principal = Depends(readers),
               session: Session = Depends(get_session)):
    return _queue_operation(session, code, name, principal, data, "apply")


@router.post("/projects/{code}/stands/{name}/test-data/inspect", response_model=QueuedOut, status_code=201)
def inspect_data(code: str, name: str, data: InspectIn, principal: Principal = Depends(readers),
                 session: Session = Depends(get_session)):
    return _queue_operation(session, code, name, principal, data, "inspect")


@router.get("/projects/{code}/test-data/jobs/{job_id}", response_model=DataJobOut)
def data_job(code: str, job_id: int, principal: Principal = Depends(readers), session: Session = Depends(get_session)):
    _project(session, code)
    job = session.get(Job, job_id)
    if job is None or job.project != code:
        raise HTTPException(404, "операция не найдена")
    operations = session.scalars(select(DataOperation).where(DataOperation.job_id == job_id)
                                 .order_by(DataOperation.position, DataOperation.id)).all()
    if not operations:
        raise HTTPException(404, "операция не найдена")
    result = None
    if job.status in ("done", "failed"):
        if (job.spec or {}).get("data_operation"):
            result = operations[0].result
        else:
            result = {"operations": [{"dataset": op.request.get("dataset"), "target": op.request["target"],
                "operation": op.request["operation"], "result": op.result} for op in operations]}
    return DataJobOut(id=job.id, status=job.status, message=job.message, result=result)


@router.get("/projects/{code}/test-data/jobs/{job_id}/result", response_model=dict)
def job_result_content(code: str, job_id: int, principal: Principal = Depends(readers),
                       session: Session = Depends(get_session)):
    result = data_job(code, job_id, principal, session).result
    if result is None:
        raise HTTPException(409, "результат ещё недоступен")
    return json_response(result)


class ResultIn(VersionIn):
    pass


@router.put("/agent/jobs/{job_id}/test-data/{operation_id}/result", status_code=204)
def store_result(job_id: int, operation_id: int, data: ResultIn, principal: Principal = Depends(require(AGENT)),
                 session: Session = Depends(get_session)):
    from .jobs import _agent_job

    job = _agent_job(session, job_id, principal)
    operation = session.get(DataOperation, operation_id)
    if operation is None or operation.job_id != job_id:
        raise HTTPException(404, "операция не найдена")
    if job.cancel_requested:
        raise HTTPException(409, "операция отменена")
    if operation.result is not None:
        raise HTTPException(409, "результат уже записан")
    operation.result = data.data
    job.heartbeat = now()
    session.commit()


def agent_operations(session, job):
    return [{"id": op.id, "request": op.request, "adapter": op.adapter}
            for op in session.scalars(select(DataOperation).where(DataOperation.job_id == job.id)
                                      .order_by(DataOperation.position, DataOperation.id))]
