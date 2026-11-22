"""Настройки установки. Всё, что зависит от клиента, приходит из окружения и файла проектов."""
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml


@dataclass(frozen=True)
class StandParam:
    name: str  # переменная окружения команды
    title: str
    default: str = ""


@dataclass(frozen=True)
class StandConfig:
    """Манифест стенда (ADR 0002, п. 5): портал знает только команды, устройство стенда - нет."""
    name: str
    title: str
    description: str = ""  # Markdown
    problem: str = ""  # Markdown: известная проблема, стенд помечается нерабочим, пока текст не убран
    environments: list[str] = field(default_factory=list)  # окружения тестов на этом стенде: общая блокировка
    links: list[dict] = field(default_factory=list)  # [{title, url}]
    workdir: str = ""  # каталог на машине агента, где выполняются команды
    commands: dict[str, str] = field(default_factory=dict)  # действие -> команда оболочки
    params: list[StandParam] = field(default_factory=list)  # параметры пересборки (deploy)
    # Доступы (базы и т. п.): [{title, value, secret}], в value - ${ПЕРЕМЕННАЯ} из secrets_files. Видят только access_roles.
    access: list[dict] = field(default_factory=list)
    access_roles: list[str] = field(default_factory=lambda: ["admin", "tester", "developer"])
    secrets_files: list[str] = field(default_factory=list)  # файлы KEY=VALUE на машине портала
    info_file: str = ""  # JSON со сведениями о сборке и проверкой стенда, пишет сам стенд
    smoke: dict = field(default_factory=dict)  # {suite, env}: прогон после пересборки, перезапуска и сброса
    seed_info: str = ""  # Markdown: какие данные засевает действие seed - показывается на странице засева
    test_data: dict[str, dict] = field(default_factory=dict)  # доверенные адаптеры установки
    source: str = "yaml"
    state: str | None = None
    build_info: dict | None = None
    verification: dict[str, str] = field(default_factory=dict)
    mode: str | None = None
    expires: datetime | None = None
    target_host: str | None = None
    output_bindings: dict[str, str] = field(default_factory=dict)
    output_binding_titles: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectConfig:
    code: str
    title: str
    results: Path | None  # клон репозитория результатов; None для проектов только с историей в БД
    tests: Path | None = None  # клон репозитория тестов: откуда берётся список наборов
    environments: list[str] = field(default_factory=list)  # окружения, разрешённые для запуска из портала
    tests_url: str = ""
    results_url: str = ""
    stands: list[StandConfig] = field(default_factory=list)
    # Исполнитель прогонов: newman (движок test-engine в образе) или pytest (на машине агента, напр. Appium с телефоном).
    runner: str = "newman"
    capability: str = ""  # возможность агента для прогонов проекта; по умолчанию - имя исполнителя
    source: str = "yaml"

    @property
    def run_capability(self) -> str:
        return self.capability or self.runner

    def stand_of(self, env: str) -> str | None:
        return next((s.name for s in self.stands if env in s.environments), None)

    def can_run_stand(self, stand: StandConfig) -> bool:
        return (self.runner == "newman" and self.tests is not None and stand.source == "database"
                and stand.state in ("ready", "limited") and bool(stand.target_host and stand.build_info and stand.output_bindings))


def database_url() -> str:
    return os.environ.get("PORTAL_DATABASE_URL", "sqlite:///./portal.db")


def static_dir() -> Path:
    return Path(os.environ.get("PORTAL_STATIC", Path(__file__).parent.parent.parent / "frontend" / "dist"))


def cookie_secure() -> bool:
    """true - когда портал за HTTPS."""
    return os.environ.get("PORTAL_COOKIE_SECURE", "false").lower() == "true"


def artifacts_dir() -> Path:
    return Path(os.environ.get("PORTAL_ARTIFACTS", "artifacts"))


def git_credentials_dir() -> Path:
    configured = os.environ.get("PORTAL_GIT_CREDENTIALS_DIR")
    return (Path(configured) if configured else artifacts_dir() / ".repository-secrets").absolute()


def _stand(name: str, item: dict) -> StandConfig:
    return StandConfig(
        name=name,
        title=item.get("title", name),
        description=item.get("description", ""),
        environments=item.get("environments") or [],
        links=[{"title": link.get("title", link["url"]), "url": link["url"]} for link in item.get("links") or []],
        workdir=item.get("workdir", ""),
        commands={action: str(command) for action, command in (item.get("commands") or {}).items()},
        params=[StandParam(p["name"], p.get("title", p["name"]), str(p.get("default", ""))) for p in item.get("params") or []],
        problem=item.get("problem") or "",
        access=[{"title": a.get("title", ""), "value": str(a.get("value", "")), "secret": bool(a.get("secret"))}
                for a in item.get("access") or []],
        access_roles=item.get("access_roles") or ["admin", "tester", "developer"],
        secrets_files=item.get("secrets_files") or [],
        info_file=item.get("info_file", ""),
        smoke=item.get("smoke") or {},
        seed_info=item.get("seed_info") or "",
        test_data=item.get("test_data") or {},
    )


def load_projects(path: str | Path | None = None) -> list[ProjectConfig]:
    """Файл проектов: projects: {<КОД>: {title, results, ..., stands}}. Относительный results считается от самого файла."""
    file = Path(path or os.environ.get("PORTAL_PROJECTS", "projects.yaml"))
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return [_project(code, item, file) for code, item in (data.get("projects") or {}).items()]


def _project(code: str, item: dict, file: Path) -> ProjectConfig:
    """Один YAML-проект; реестр обрабатывает ошибки соседних проектов независимо."""
    return ProjectConfig(
        code,
        item.get("title", code),
        (file.parent / item["results"]).resolve(),
        (file.parent / item["tests"]).resolve() if item.get("tests") else None,
        item.get("environments") or [],
        item.get("tests_url", ""),
        item.get("results_url", ""),
        [_stand(name, value or {}) for name, value in (item.get("stands") or {}).items()],
        item.get("runner") or "newman",
        item.get("capability") or "",
    )
