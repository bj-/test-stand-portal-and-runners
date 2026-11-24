"""Queue the ordinary host cleanup for expired managed stands."""
import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .auth import now
from .db import SessionLocal
from .jobs import _claim_lock
from .managed_stands import _mutation_lock, _queue_deletion, _stand_locked
from .models import Job, StandInstance
from .registry import load_registry
from .update_control import maintenance_active

AUTOMATIC_ACTOR = "system:stand-expiry"
logger = logging.getLogger(__name__)


def queue_expired(session: Session, moment: datetime) -> int:
    """Enqueue once under the same database locks as a manual deletion."""
    _mutation_lock(session)
    _claim_lock(session)
    if maintenance_active():
        session.rollback()
        return 0
    local_date = moment.replace(tzinfo=UTC).astimezone().date()
    local_midnight_utc = datetime.combine(local_date, time.min).astimezone(UTC).replace(tzinfo=None)
    deletion_exists = select(Job.id).where(
        Job.project == StandInstance.project, Job.target == StandInstance.id, Job.kind == "stand-delete",
        or_(Job.status.in_(("queued", "running")), Job.created_by == AUTOMATIC_ACTOR),
    ).exists()
    due = session.scalars(select(StandInstance).where(
        StandInstance.mode == "ephemeral", StandInstance.deleted_at.is_(None), ~deletion_exists,
        or_(StandInstance.delete_after <= moment,
            and_(StandInstance.delete_after.is_(None), StandInstance.expires < local_midnight_utc)),
    ).order_by(StandInstance.expires, StandInstance.id)).all()
    count = 0
    if due:
        projects = load_registry(session).projects
        for row in due:
            project = projects.get(row.project)
            if project is None or _stand_locked(session, project, row):
                continue
            _queue_deletion(session, row, created_by=AUTOMATIC_ACTOR,
                            title="Удалить стенд по сроку: " + row.name)
            count += 1
    session.commit()
    return count


def _sweep() -> None:
    with SessionLocal() as session:
        queue_expired(session, now())


def _seconds_until_midnight() -> float:
    current = datetime.now()
    midnight = datetime.combine(current.date() + timedelta(days=1), time.min)
    # Local calendar days can contain 23 or 25 hours when the clocks change.
    return midnight.timestamp() - current.timestamp()


async def run(stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_seconds_until_midnight())
            return
        except TimeoutError:
            pass
        try:
            await asyncio.to_thread(_sweep)
        except Exception:
            logger.exception("Не удалось проверить сроки временных стендов")
