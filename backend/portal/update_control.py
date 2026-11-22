"""Общий файловый протокол обновлений и остановка выдачи новых задач.

Host updater owns status/operations. Portal publishes requests only. Maintenance
has no expiry: only an explicit resume permits top-level claims again.
"""
import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select


def control_dir() -> Path | None:
    value = os.environ.get("PORTAL_UPDATE_DIR", "").strip()
    return Path(value) if value else None


def maintenance_active() -> bool:
    directory = control_dir()
    # Malformed contents must not reopen the queue; presence alone is the flag.
    return directory is not None and (directory / "maintenance.json").exists()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def maintenance(action: str, session) -> dict:
    # Local imports keep the claim path independent of CLI/db initialization.
    from .jobs import _claim_lock
    from .models import Job

    if action not in {"drain", "idle", "resume"}:
        raise ValueError("неизвестная команда")
    directory = control_dir()
    if directory is None:
        raise ValueError("PORTAL_UPDATE_DIR не задан")
    _claim_lock(session)
    if action == "drain":
        atomic_json(directory / "maintenance.json", {"created_at": datetime.now(UTC).isoformat()})
    elif action == "resume":
        (directory / "maintenance.json").unlink(missing_ok=True)
    # Do not expire heartbeats here: every running job has to finish naturally.
    running = session.scalar(select(func.count()).select_from(Job).where(Job.status == "running"))
    result = {"running": running, "maintenance": maintenance_active()}
    session.commit()
    return result


def main() -> None:
    from .db import SessionLocal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("drain", "idle", "resume"))
    args = parser.parse_args()
    with SessionLocal() as session:
        print(json.dumps(maintenance(args.action, session)))


if __name__ == "__main__":
    main()
