"""Единый, только читающий реестр конфигурации YAML и БД.

Строки Project с configuration=None остаются индексом истории. Настроенный
проект БД никогда не перекрывает одноимённый проект YAML автоматически.
"""
import os
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import ProjectConfig, StandConfig, _project
from .models import Job, Project, StandBuild, StandInstance, StandTemplate
from .stand_contract import LIVENESS_STAGE, binding_titles


class RegistryError(ValueError):
    """Безопасная ошибка без исходных конфигураций, путей и секретов."""

    def __str__(self) -> str:
        message = super().__str__()
        code = message.removeprefix("registry: ")
        reason = {
            "projects_file_unavailable": "файл проектов недоступен или содержит ошибку YAML",
            "invalid_projects_file": "неверная структура файла проектов",
            "database_unavailable": "база данных недоступна",
            "invalid_code": "неверный код проекта",
            "invalid_configuration": "неверная конфигурация проекта",
            "configuration_conflict": "конфликт источников конфигурации",
            "invalid_environments": "неверный список окружений",
            "duplicate_environments": "окружение указано повторно или принадлежит нескольким стендам",
            "invalid_stand": "неверная конфигурация стенда",
            "duplicate_stand": "конфликт имени или идентификатора стенда",
        }.get(code)
        if reason is None:
            return message
        prefix = "Реестр: " if message.startswith("registry: ") else ""
        return f"{prefix}{reason} ({code})"


@dataclass(frozen=True)
class ProjectRegistry:
    projects: dict[str, ProjectConfig]
    errors: dict[str, str]
    summaries: list[dict[str, str]]

    def get(self, code: str) -> ProjectConfig:
        if code in self.errors:
            raise RegistryError(self.errors[code])
        return self.projects[code]


_FIELDS = frozenset({"results", "tests", "tests_url", "results_url", "environments", "runner", "capability"})


def _valid_code(code: object) -> bool:
    # Старые проекты не ограничивались ASCII; длина соответствует projects.code.
    return (isinstance(code, str) and 0 < len(code) <= 32 and bool(code.strip())
            and code.isprintable() and "/" not in code and "\\" not in code)


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in keys:
                    raise yaml.YAMLError("duplicate_key")
                keys.add(key)
            except TypeError:
                raise yaml.YAMLError("invalid_key") from None
        return super().construct_mapping(node, deep=deep)


def _error(code: str) -> RegistryError:
    return RegistryError(code)


def _environments(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(env, str) or not env.strip() for env in value):
        raise _error("invalid_environments")
    if len(set(value)) != len(value):
        raise _error("duplicate_environments")
    return list(value)


def _db_project(row: Project) -> ProjectConfig:
    settings = row.configuration
    if not isinstance(settings, dict) or settings.keys() - _FIELDS:
        raise _error("invalid_configuration")
    paths = {}
    for key in ("results", "tests"):
        value = settings.get(key)
        if value is None:
            paths[key] = None
        elif isinstance(value, str) and value.strip() and Path(value).is_absolute():
            paths[key] = Path(value).resolve()
        else:
            raise _error("invalid_configuration")
    strings = {}
    for key, default in (("tests_url", ""), ("results_url", ""), ("runner", "newman"), ("capability", "")):
        value = settings.get(key, default)
        if not isinstance(value, str) or (key == "runner" and not value.strip()):
            raise _error("invalid_configuration")
        strings[key] = value
    return ProjectConfig(code=row.code, title=row.title, **paths, **strings,
                         environments=_environments(settings.get("environments", [])), source="database")


def _managed_stand(row: StandInstance, builds=None, template_titles=None, failed_results=None) -> StandConfig:
    build = (builds or {}).get(row.deployed_build_id)
    info, links, result = None, [], {}
    outputs, titles = {}, {}
    if build and build.project == row.project and build.stand_id == row.id and build.status == "done":
        result = build.result or {}
        statuses = {stage["name"]: stage["status"] for stage in result.get("stages", [])}
        health = [{"name": "healthcheck", "ok": True}, {"name": "smoke", "ok": statuses.get("smoke") != "failed"}]
        if statuses.get(LIVENESS_STAGE) == "success":
            health.append({"name": LIVENESS_STAGE, "ok": True})
        info = {"build_id": build.id, "components": [{"name": c["id"], "commit": c["sha"]}
                for c in build.snapshot.get("components", [])], "images": build.images,
                "built": next((s.get("finished_at") for s in result.get("stages", []) if s.get("name") == "build"), None),
                "deployed": build.finished.isoformat() + "Z" if build.finished else None,
                "checked": build.finished.isoformat() + "Z" if build.finished else None,
                "health": health}
        titles = binding_titles(build.snapshot.get("template", {}), (template_titles or {}).get(
            (build.project, build.template_key, build.template_version)))
        outputs = result.get("output_bindings", {})
        links = [{"title": titles.get(name, name), "url": url} for name, url in outputs.items()]
        if "allure-report.html" in (build.artifact_refs or []):
            links.append({"title": "Allure развёрнутой сборки", "url": f"/api/stand-builds/{build.id}/artifacts/allure-report.html"})
    state_title = {"created": "создан", "waiting": "ожидает исполнителя", "building": "собирается",
                   "deploying": "разворачивается", "checking": "проверяется", "ready": "готов",
                   "limited": "работает с ограничениями",
                   "failed": "ошибка сборки", "stopped": "остановлен"}.get(row.status, row.status)
    if row.status == "failed":
        result = (failed_results or {}).get(row.id) or {}
    verification = {s["name"]: s["status"] for s in result.get("stages", []) if s["name"] in ("smoke", LIVENESS_STAGE)}
    return StandConfig(
        name=row.id, title=row.name, source="database", state=row.status,
        environments=_environments(row.environments), access_roles=[],
        description="Настраиваемый стенд. Состояние: " + state_title,
        build_info=info, links=links, verification=verification,
        mode=row.mode, expires=row.expires.replace(tzinfo=UTC) if row.expires else None,
        target_host=row.target_host, output_bindings=outputs, output_binding_titles=titles,
    )


def _merge_stands(project: ProjectConfig, rows: list[StandInstance], builds=None, template_titles=None,
                  failed_results=None) -> ProjectConfig:
    if any(not isinstance(value, str) for value in
           (project.title, project.tests_url, project.results_url, project.runner, project.capability)):
        raise _error("invalid_configuration")
    environments = _environments(project.environments)
    names = set()
    owners = set()
    for stand in project.stands:
        if not isinstance(stand.name, str) or not stand.name.strip() or any(
            not isinstance(value, str) for value in (stand.title, stand.description, stand.problem, stand.seed_info)
        ):
            raise _error("invalid_stand")
        if stand.name in names:
            raise _error("duplicate_stand")
        names.add(stand.name)
        for env in _environments(stand.environments):
            if env in owners:
                raise _error("duplicate_environments")
            owners.add(env)
    managed = []
    for row in rows:
        # Оба имени проверяются: маршруты используют ID, человек видит name.
        if row.id in names or row.name in names:
            raise _error("duplicate_stand")
        stand = _managed_stand(row, builds, template_titles, failed_results)
        for env in stand.environments:
            if env in owners:
                raise _error("duplicate_environments")
            owners.add(env)
            if env not in environments:
                environments.append(env)
        managed.append(stand)
    return replace(project, stands=[*project.stands, *managed], environments=environments)


def _yaml_projects(path: str | Path | None) -> tuple[Path, dict]:
    configured = path is not None or "PORTAL_PROJECTS" in os.environ
    file = Path(path if path is not None else os.environ.get("PORTAL_PROJECTS", "projects.yaml"))
    try:
        data = yaml.load(file.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except FileNotFoundError:
        if not configured:
            return file, {}
        raise RegistryError("registry: projects_file_unavailable") from None
    except (OSError, UnicodeError, yaml.YAMLError):
        raise RegistryError("registry: projects_file_unavailable") from None
    if data is None:
        data = {}
    if not isinstance(data, dict) or (data.get("projects") is not None and not isinstance(data["projects"], dict)):
        raise RegistryError("registry: invalid_projects_file")
    return file, data.get("projects") or {}


def load_registry(session: Session, path: str | Path | None = None) -> ProjectRegistry:
    """Читает только переданную сессию, без flush, импортов и кэша между запросами."""
    file, yaml_projects = _yaml_projects(path)
    with session.no_autoflush:
        try:
            db_projects = {row.code: row for row in session.scalars(select(Project).order_by(Project.code))}
            instances: dict[str, list[StandInstance]] = {}
            for row in session.scalars(select(StandInstance).where(StandInstance.deleted_at.is_(None)).order_by(StandInstance.project, StandInstance.id)):
                instances.setdefault(row.project, []).append(row)
            deployed_ids = [row.deployed_build_id for rows in instances.values() for row in rows if row.deployed_build_id]
            builds = {build.id: build for build in session.scalars(select(StandBuild).where(StandBuild.id.in_(deployed_ids)))} if deployed_ids else {}
            failed_ids = [row.id for rows in instances.values() for row in rows if row.status == "failed"]
            failed_results = {}
            if failed_ids:
                latest_jobs = select(func.max(Job.id)).where(Job.kind == "stand-build", Job.target.in_(failed_ids)).group_by(Job.target)
                failed_results = dict(session.execute(select(StandBuild.stand_id, StandBuild.result).where(
                    StandBuild.stand_id.in_(failed_ids), StandBuild.job_id.in_(latest_jobs))).all())
            template_titles = {
                (row.project, row.key, row.version): row.output_binding_titles
                for row in session.scalars(select(StandTemplate).where(
                    StandTemplate.project.in_(list(instances)), StandTemplate.output_binding_titles.is_not(None)))
            } if builds else {}
            return _registry(file, yaml_projects, db_projects, instances, builds, template_titles, failed_results)
        except SQLAlchemyError:
            raise RegistryError("registry: database_unavailable") from None


def _registry(file: Path, yaml_projects: dict, db_projects: dict, instances: dict, builds=None, template_titles=None,
              failed_results=None) -> ProjectRegistry:
    projects = {}
    errors = {}
    summaries = []
    codes = set(yaml_projects) | set(db_projects)
    if any(not _valid_code(code) for code in codes):
        # Не отражаем значения некорректных ключей в публичной диагностике.
        errors["<invalid>"] = str(_error("invalid_code"))
    for code in sorted(code for code in codes if _valid_code(code)):
        row = db_projects.get(code)
        item = yaml_projects.get(code)
        title = item.get("title", code) if code in yaml_projects and isinstance(item, dict) else (
            row.title if row is not None else code
        )
        summaries.append({"code": code, "title": title if isinstance(title, str) else code})
        try:
            if code in yaml_projects:
                if row is not None and row.configuration is not None:
                    raise _error("configuration_conflict")
                project = _project(code, item, file)
            elif row.configuration is not None:
                project = _db_project(row)
            else:
                project = ProjectConfig(code, row.title, None, source="index")
            projects[code] = _merge_stands(project, instances.get(code, []), builds, template_titles, failed_results)
        except RegistryError as exc:
            errors[code] = f"Проект {code}: {exc}"
        except (TypeError, ValueError, KeyError, AttributeError, OSError):
            errors[code] = f"Проект {code}: {_error('invalid_configuration')}"
    return ProjectRegistry(projects, errors, summaries)
