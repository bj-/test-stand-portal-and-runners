"""Модель данных. Прогоны и тесты - индекс файлов результатов (ADR 0002, п. 6): пересобираются командой sync."""
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Json = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    configuration: Mapped[dict | None] = mapped_column(Json, nullable=True)
    stand_data_bundle: Mapped[dict | None] = mapped_column(Json, nullable=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), index=True)
    suite: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str | None] = mapped_column(String(40))
    modules: Mapped[list] = mapped_column(Json, default=list)
    started: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    finished: Mapped[datetime | None] = mapped_column(DateTime)
    who: Mapped[str | None] = mapped_column(String(200))
    trigger: Mapped[str | None] = mapped_column(String(40))
    stand_name: Mapped[str | None] = mapped_column(String(200))
    stand_url: Mapped[str | None] = mapped_column(String(300))
    stand_kind: Mapped[str | None] = mapped_column(String(40))
    build: Mapped[str | None] = mapped_column(String(200))
    tests_branch: Mapped[str | None] = mapped_column(String(200))
    tests_commit: Mapped[str | None] = mapped_column(String(64))
    engine_version: Mapped[str | None] = mapped_column(String(40))
    runner: Mapped[str | None] = mapped_column(String(80))
    gate_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    tests: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    no_checks: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    checks: Mapped[int] = mapped_column(Integer, default=0)
    checks_failed: Mapped[int] = mapped_column(Integer, default=0)
    bug_ids: Mapped[list] = mapped_column(Json, default=list)
    path: Mapped[str] = mapped_column(String(300))  # относительно клона результатов
    source_hash: Mapped[str] = mapped_column(String(64))

    results: Mapped[list["TestResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="TestResult.position"
    )


class TestResult(Base):
    __tablename__ = "test_results"
    __table_args__ = (Index("ix_test_results_path", "path"),)
    __test__ = False  # не тестовый класс для pytest

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(String(16))
    url: Mapped[str | None] = mapped_column(Text)
    code: Mapped[int | None] = mapped_column(Integer)
    time_ms: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(20), index=True)
    gate: Mapped[bool] = mapped_column(Boolean, default=False)
    groups: Mapped[list] = mapped_column(Json, default=list)

    run: Mapped[Run] = relationship(back_populates="results")


class Bug(Base):
    __tablename__ = "bugs"
    __table_args__ = (UniqueConstraint("project", "number"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # BUG-<КОД>-NNNN
    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    module: Mapped[str | None] = mapped_column(String(200))
    type: Mapped[str | None] = mapped_column(String(200))
    severity: Mapped[str | None] = mapped_column(String(40), index=True)
    priority: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str | None] = mapped_column(String(40), index=True)
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    groups: Mapped[list] = mapped_column(Json, default=list)
    stand: Mapped[str | None] = mapped_column(Text)
    found_date: Mapped[str | None] = mapped_column(String(10))
    run_id: Mapped[str | None] = mapped_column(String(120))  # без внешнего ключа: прогон мог уйти в архив
    tracker: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")  # публичная часть
    restricted_body: Mapped[str | None] = mapped_column(Text)  # только для людей, ADR 0002, п. 8
    source_hash: Mapped[str] = mapped_column(String(64))


class User(Base):
    __tablename__ = "users"

    login: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_change_suggested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    email: Mapped[str] = mapped_column(String(200), default="", server_default="")
    telegram: Mapped[str] = mapped_column(String(64), default="", server_default="")
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    theme: Mapped[str] = mapped_column(String(10), default="", server_default="")  # "" - по умолчанию (тёмная)


class LoginSession(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    login: Mapped[str] = mapped_column(ForeignKey("users.login", ondelete="CASCADE"), index=True)
    expires: Mapped[datetime] = mapped_column(DateTime)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    stand_host: Mapped[str | None] = mapped_column(String(128))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), index=True)  # queued, running, done, failed, cancelled
    target: Mapped[str] = mapped_column(String(200), index=True)  # стенд: на одном стенде - одна задача
    title: Mapped[str] = mapped_column(String(300))
    spec: Mapped[dict] = mapped_column(Json)
    created_by: Mapped[str] = mapped_column(String(120))
    created: Mapped[datetime] = mapped_column(DateTime)
    started: Mapped[datetime | None] = mapped_column(DateTime)
    finished: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime)
    agent: Mapped[str | None] = mapped_column(String(120))
    run_id: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("project", "key", name="uq_datasets_project_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    format: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(120))
    created: Mapped[datetime] = mapped_column(DateTime)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(Json)
    created_by: Mapped[str] = mapped_column(String(120))
    created: Mapped[datetime] = mapped_column(DateTime)


class DataBinding(Base):
    __tablename__ = "data_bindings"
    __table_args__ = (ForeignKeyConstraint(["dataset_id", "version"],
        ["dataset_versions.dataset_id", "dataset_versions.version"], name="fk_binding_version"),
        UniqueConstraint("project", "stand", "target", "dataset_id", name="uq_binding_dataset_target"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), index=True)
    stand: Mapped[str] = mapped_column(String(200))
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    version: Mapped[int] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_apply: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class DataOperation(Base):
    """Закрытый снимок запроса и результата; никогда не сериализуется через общие jobs."""
    __tablename__ = "data_operations"
    __table_args__ = (UniqueConstraint("job_id", "position", name="uq_data_operations_job_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    request: Mapped[dict] = mapped_column(Json)
    adapter: Mapped[dict] = mapped_column(Json)
    result: Mapped[dict | None] = mapped_column(Json, nullable=True)


def _stand_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class GitServer(Base):
    __tablename__ = "git_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(2048))
    provider: Mapped[str] = mapped_column(String(16))


class StandRepository(Base):
    """Каталог разрешённых repository IDs; пути clones задаются на целевом хосте."""
    __tablename__ = "stand_repositories"
    __table_args__ = (CheckConstraint("length(trim(key)) > 0", name="ck_stand_repositories_key"),)

    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    components: Mapped[list] = mapped_column(Json, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    git_url: Mapped[str | None] = mapped_column(String(2048))
    git_server_id: Mapped[str | None] = mapped_column(ForeignKey("git_servers.id", name="fk_stand_repositories_git_server"))


class StandTemplate(Base):
    """Одна версия доверенного шаблона; spec хранит portable template без секретов."""
    __tablename__ = "stand_templates"
    __table_args__ = (
        CheckConstraint("length(trim(key)) > 0", name="ck_stand_templates_key"),
        CheckConstraint("length(trim(version)) > 0", name="ck_stand_templates_version"),
    )

    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    spec: Mapped[dict] = mapped_column(Json)
    output_binding_titles: Mapped[dict | None] = mapped_column(Json, nullable=True)
    name_ref_exclusions: Mapped[list | None] = mapped_column(Json, nullable=True)
    technical_components: Mapped[list | None] = mapped_column(Json, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=_stand_now)


class StandInstance(Base):
    __tablename__ = "stand_instances"
    __table_args__ = (
        ForeignKeyConstraint(["project", "template_key", "template_version"],
            ["stand_templates.project", "stand_templates.key", "stand_templates.version"],
            name="fk_stand_instances_template"),
        Index("uq_stand_instances_project_name", "project", "name", unique=True,
              sqlite_where=text("deleted_at IS NULL"), postgresql_where=text("deleted_at IS NULL")),
        UniqueConstraint("project", "request_key", name="uq_stand_instances_project_request"),
        UniqueConstraint("id", "project", name="uq_stand_instances_id_project"),
        UniqueConstraint("id", "target_host", name="uq_stand_instances_id_host"),
        CheckConstraint("length(trim(id)) > 0", name="ck_stand_instances_id"),
        CheckConstraint("length(trim(name)) > 0", name="ck_stand_instances_name"),
        CheckConstraint("length(trim(target_host)) > 0", name="ck_stand_instances_host"),
        CheckConstraint("request_key IS NULL OR length(trim(request_key)) > 0", name="ck_stand_instances_request"),
        CheckConstraint("mode IN ('permanent', 'ephemeral')", name="ck_stand_instances_mode"),
        CheckConstraint("(mode = 'permanent' AND expires IS NULL) OR "
            "(mode = 'ephemeral' AND expires IS NOT NULL AND expires > created)", name="ck_stand_instances_expiry"),
        CheckConstraint("status IN ('created', 'waiting', 'building', 'deploying', 'checking', "
                        "'ready', 'limited', 'failed', 'stopped')", name="ck_stand_instances_status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    template_key: Mapped[str] = mapped_column(String(128))
    template_version: Mapped[str] = mapped_column(String(128))
    owner_login: Mapped[str] = mapped_column(ForeignKey("users.login"), index=True)
    target_host: Mapped[str] = mapped_column(String(128), index=True)  # стабильный ID хоста, не имя процесса agent
    mode: Mapped[str] = mapped_column(String(20), default="permanent")
    expires: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    profile: Mapped[str] = mapped_column(String(128))
    refs: Mapped[dict] = mapped_column(Json, default=dict)
    environments: Mapped[list] = mapped_column(Json, default=list, server_default="[]")
    parameters: Mapped[dict] = mapped_column(Json, default=dict)
    resources: Mapped[dict] = mapped_column(Json, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="created", index=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=_stand_now)
    updated: Mapped[datetime] = mapped_column(DateTime, default=_stand_now, onupdate=_stand_now)
    request_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    deployed_build_id: Mapped[str | None] = mapped_column(String(128))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class StandBuild(Base):
    """Snapshot portable v1 отдельно от меняющихся результатов исполнения.

    JSON shape, полные SHA и отсутствие секретов проверит будущий сервис записи.
    Сама схема не запрещает UPDATE snapshot и не гарантирует его неизменность.
    """
    __tablename__ = "stand_builds"
    __table_args__ = (
        ForeignKeyConstraint(["stand_id", "project"], ["stand_instances.id", "stand_instances.project"],
            name="fk_stand_builds_instance"),
        ForeignKeyConstraint(["project", "template_key", "template_version"],
            ["stand_templates.project", "stand_templates.key", "stand_templates.version"],
            name="fk_stand_builds_template"),
        CheckConstraint("length(trim(id)) > 0", name="ck_stand_builds_id"),
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed', 'cancelled')", name="ck_stand_builds_status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project: Mapped[str] = mapped_column(ForeignKey("projects.code"), index=True)
    stand_id: Mapped[str] = mapped_column(String(128), index=True)
    template_key: Mapped[str] = mapped_column(String(128))
    template_version: Mapped[str] = mapped_column(String(128))
    snapshot: Mapped[dict] = mapped_column(Json)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    result: Mapped[dict | None] = mapped_column(Json, nullable=True)
    images: Mapped[list] = mapped_column(Json, default=list)
    log_refs: Mapped[list] = mapped_column(Json, default=list)
    artifact_refs: Mapped[list] = mapped_column(Json, default=list)
    created: Mapped[datetime] = mapped_column(DateTime, default=_stand_now)
    started: Mapped[datetime | None] = mapped_column(DateTime)
    finished: Mapped[datetime | None] = mapped_column(DateTime)


class StandPortReservation(Base):
    __tablename__ = "stand_port_reservations"
    __table_args__ = (
        ForeignKeyConstraint(["stand_id", "target_host"], ["stand_instances.id", "stand_instances.target_host"],
            name="fk_stand_ports_instance_host"),
        UniqueConstraint("target_host", "protocol", "port", name="uq_stand_ports_host_protocol_port"),
        CheckConstraint("protocol IN ('tcp', 'udp')", name="ck_stand_ports_protocol"),
        CheckConstraint("port >= 1 AND port <= 65535", name="ck_stand_ports_port"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stand_id: Mapped[str] = mapped_column(String(128), index=True)
    target_host: Mapped[str] = mapped_column(String(128))
    protocol: Mapped[str] = mapped_column(String(3))
    port: Mapped[int] = mapped_column(Integer)
    bind_address: Mapped[str | None] = mapped_column(String(128))  # не участвует в уникальности порта
