"""Managed stand actions use host-bound jobs and the existing frozen retry flow."""
from fastapi import HTTPException
from sqlalchemy import select

from .auth import ADMIN, DEVELOPER, TESTER, cookie_user, now
from .models import Job, StandBuild, StandInstance
from .stands import ACTIONS, ActionOut

MANAGED_ACTIONS = ("deploy", "restart", "status", "logs")


def latest_build(session, code, instance_id):
    return session.scalar(select(StandBuild).where(StandBuild.project == code,
        StandBuild.stand_id == instance_id).order_by(StandBuild.created.desc(), StandBuild.id.desc()).limit(1))


def managed_actions(session, code, stand, principal, request):
    instance = session.get(StandInstance, stand.name)
    if instance is None or instance.project != code or instance.deleted_at is not None:
        return []
    from .stands import deleting_stands

    available = stand.name not in deleting_stands(session, code)
    build = latest_build(session, code, stand.name)
    if build is None:
        return []
    browser_owner = False
    if principal.role == DEVELOPER and not principal.is_guest:
        try:
            browser_owner = cookie_user(request, session).login == instance.owner_login
        except HTTPException:
            pass
    manage = principal.role in (ADMIN, TESTER) or browser_owner
    return [ActionOut(name=name, title=ACTIONS[name].title, destructive=False,
        allowed=bool(available and not principal.is_guest and (
            manage if name in ("deploy", "restart") else principal.role in ACTIONS[name].roles)))
        for name in MANAGED_ACTIONS]


def queue_action(session, project, stand, action, data, principal, request):
    from .jobs import _claim_lock
    from .managed_stands import _commit, _mutation_lock
    from .stand_execution import available_instance, queue_rebuild, require_owner

    if action not in MANAGED_ACTIONS:
        raise HTTPException(404, "у стенда нет такого действия")
    roles = (ADMIN, TESTER, DEVELOPER) if action in ("deploy", "restart") else ACTIONS[action].roles
    if principal.role not in roles:
        raise HTTPException(403, "недостаточно прав для этого действия")
    if data.params:
        raise HTTPException(422, "управляемое действие использует сохранённые версии без параметров")
    owner = cookie_user(request, session) if principal.role == DEVELOPER and action in ("deploy", "restart") else None
    _mutation_lock(session)
    _claim_lock(session)
    try:
        instance = available_instance(session, project.code, stand.name)
        if owner:
            require_owner(instance, principal, owner)
        build = latest_build(session, project.code, instance.id)
        if build is None:
            raise HTTPException(409, "сначала дождитесь подготовки первой сборки стенда")
        if action == "deploy":
            job = queue_rebuild(session, project, instance, build.id, principal.name)
        else:
            job = Job(project=project.code, kind="stand-control", status="queued", target=instance.id,
                title=ACTIONS[action].title + ": " + instance.name,
                created_by=owner.login if owner else principal.name, created=now(),
                spec={"schema_version": 1, "project": project.code, "stand": instance.id,
                      "target_host": instance.target_host, "action": action, "lock": ACTIONS[action].lock})
            session.add(job)
        _commit(session)
        return {"id": job.id, "message": "Действие поставлено в очередь. Результат появится в журнале задачи."}
    except Exception:
        session.rollback()
        raise


def require_control_agent(session, job, principal):
    """Log and finish keep checking the same live host binding as job claiming."""
    from .stand_execution import bound_agent

    token = bound_agent(session, principal)
    if token is None or token.stand_host != (job.spec or {}).get("target_host"):
        raise HTTPException(403, "агент не привязан к хосту управления")
    instance = session.get(StandInstance, job.target)
    if (instance is None or instance.project != job.project or instance.deleted_at is not None
            or instance.target_host != token.stand_host or job.spec.get("stand") != instance.id
            or job.spec.get("project") != instance.project):
        raise HTTPException(409, "управляемый стенд недоступен")
