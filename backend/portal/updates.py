"""Администратор отправляет только check/apply; команды исполняет host updater."""
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from .auth import ADMIN, UPDATER, Principal, require
from .update_control import atomic_json, control_dir, maintenance_active

router = APIRouter(prefix="/api/admin/updates", dependencies=[Depends(require(ADMIN, UPDATER))])
PHASE_MESSAGES = {
    "queued": "Запрос ожидает обработки.", "idle": "Ожидание следующей проверки.",
    "checking": "Проверка обновлений.", "available": "Доступна новая версия.",
    "up_to_date": "Установлена актуальная версия.", "draining": "Ожидание завершения задач.",
    "preparing": "Подготовка новой версии.", "backing_up": "Создание резервной копии.",
    "activating": "Установка обновления.", "resuming": "Возобновление выдачи задач.",
    "rolling_back": "Восстановление предыдущей версии.", "unchanged": "Установлена актуальная версия.",
    "succeeded": "Операция завершена.",
    "recovery_required": "Требуется восстановление администратором сервера. Выдача новых задач приостановлена.",
    "building": "Сборка новой версии.", "applying": "Установка обновления.",
    "restarting": "Перезапуск портала.", "verifying": "Проверка портала после обновления.",
    "done": "Операция завершена.", "failed": "Обновление завершилось ошибкой. Подробности доступны администратору сервера.",
    "error": "Ошибка обновления. Подробности доступны администратору сервера.",
    "rolled_back": "Восстановлена предыдущая версия.", "disabled": "Обновления не настроены.",
}
FIELDS = {"id", "action", "requested_by", "created_at", "started_at", "finished_at", "updated_at",
          "current_revision", "available_revision", "last_checked_at", "last_operation_id", "phase"}
REVISION = re.compile(r"^[a-fA-F0-9]{7,64}$")


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _directory() -> Path:
    directory = control_dir()
    if directory is None:
        raise HTTPException(503, "обновления не настроены")
    return directory


def _read(path: Path) -> dict | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(32_769)
        data = json.loads(raw) if len(raw) <= 32_768 else None
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise HTTPException(503, "состояние обновления временно недоступно") from None


def _public(data: dict) -> dict:
    result = {key: value[:200] for key, value in data.items() if key in FIELDS and isinstance(value, str)}
    for key in ("current_revision", "available_revision"):
        if key in result and not REVISION.fullmatch(result[key]):
            result.pop(key)
    phase = result.get("phase", "idle")
    result["phase"] = phase if phase in PHASE_MESSAGES else "unknown"
    # Host diagnostics/logs can contain secrets: never forward arbitrary text.
    result["message"] = PHASE_MESSAGES.get(phase, "Состояние обновления уточняется.")
    if phase in {"failed", "recovery_required", "rolling_back", "rolled_back"}:
        failure_phase = data.get("failure_phase")
        if isinstance(failure_phase, str) and failure_phase in PHASE_MESSAGES:
            result["failure_phase"] = failure_phase
        error_code = data.get("error_code")
        if isinstance(error_code, str) and error_code in {"command_timeout", "update_failed"}:
            result["error_code"] = error_code
    return result


@router.get("")
def status():
    directory = control_dir()
    if directory is None:
        return {"enabled": False, "phase": "disabled", "message": PHASE_MESSAGES["disabled"], "maintenance": False}
    data = _read(directory / "status.json") or {"phase": "idle"}
    return {**_public(data), "enabled": bool(data.get("enabled", True)), "maintenance": maintenance_active()}


def _queue(action: str, principal: Principal) -> dict:
    directory = _directory()
    request = {"id": str(uuid4()), "action": action, "requested_by": principal.name,
               "created_at": datetime.now(UTC).isoformat()}
    try:
        atomic_json(directory / "requests" / f"{request['id']}.json", request)
    except OSError:
        raise HTTPException(503, "не удалось сохранить запрос обновления") from None
    return {**request, "phase": "queued", "message": PHASE_MESSAGES["queued"]}


@router.post("/check", status_code=202)
def check(data: EmptyRequest | None = None, principal: Principal = Depends(require(ADMIN, UPDATER))):
    return _queue("check", principal)


@router.post("/apply", status_code=202)
def apply(data: EmptyRequest | None = None, principal: Principal = Depends(require(ADMIN, UPDATER))):
    return _queue("apply", principal)


@router.get("/{operation_id}")
def operation(operation_id: str):
    directory = _directory()
    try:
        identifier = str(UUID(operation_id))
    except ValueError:
        raise HTTPException(404, "операция не найдена") from None
    data = _read(directory / "operations" / f"{identifier}.json")
    if data is None:
        request = _read(directory / "requests" / f"{identifier}.json")
        data = {**request, "phase": "queued"} if request else None
    if data is None:
        raise HTTPException(404, "операция не найдена")
    return {**_public(data), "id": identifier}
