"""Каталог, экземпляры, сборки и резервации портов настраиваемых стендов."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None
Json = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "stand_repositories",
        sa.Column("project", sa.String(32), sa.ForeignKey("projects.code"), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("components", Json, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_stand_repositories_key"),
    )
    op.create_table(
        "stand_templates",
        sa.Column("project", sa.String(32), sa.ForeignKey("projects.code"), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(128), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("spec", Json, nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_stand_templates_key"),
        sa.CheckConstraint("length(trim(version)) > 0", name="ck_stand_templates_version"),
    )
    op.create_table(
        "stand_instances",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("project", sa.String(32), sa.ForeignKey("projects.code"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("template_key", sa.String(128), nullable=False),
        sa.Column("template_version", sa.String(128), nullable=False),
        sa.Column("owner_login", sa.String(80), sa.ForeignKey("users.login"), nullable=False),
        sa.Column("target_host", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("expires", sa.DateTime(), nullable=True),
        sa.Column("profile", sa.String(128), nullable=False),
        sa.Column("refs", Json, nullable=False),
        sa.Column("parameters", Json, nullable=False),
        sa.Column("resources", Json, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(["project", "template_key", "template_version"],
            ["stand_templates.project", "stand_templates.key", "stand_templates.version"],
            name="fk_stand_instances_template"),
        sa.UniqueConstraint("project", "name", name="uq_stand_instances_project_name"),
        sa.UniqueConstraint("project", "request_key", name="uq_stand_instances_project_request"),
        sa.UniqueConstraint("id", "project", name="uq_stand_instances_id_project"),
        sa.UniqueConstraint("id", "target_host", name="uq_stand_instances_id_host"),
        sa.CheckConstraint("length(trim(id)) > 0", name="ck_stand_instances_id"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_stand_instances_name"),
        sa.CheckConstraint("length(trim(target_host)) > 0", name="ck_stand_instances_host"),
        sa.CheckConstraint("request_key IS NULL OR length(trim(request_key)) > 0", name="ck_stand_instances_request"),
        sa.CheckConstraint("mode IN ('permanent', 'ephemeral')", name="ck_stand_instances_mode"),
        sa.CheckConstraint("(mode = 'permanent' AND expires IS NULL) OR "
            "(mode = 'ephemeral' AND expires IS NOT NULL AND expires > created)", name="ck_stand_instances_expiry"),
        sa.CheckConstraint("status IN ('created', 'waiting', 'building', 'deploying', 'checking', "
            "'ready', 'failed', 'stopped')", name="ck_stand_instances_status"),
    )
    for column in ("project", "owner_login", "target_host", "expires", "status"):
        op.create_index(f"ix_stand_instances_{column}", "stand_instances", [column])
    op.create_table(
        "stand_builds",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("project", sa.String(32), sa.ForeignKey("projects.code"), nullable=False),
        sa.Column("stand_id", sa.String(128), nullable=False),
        sa.Column("template_key", sa.String(128), nullable=False),
        sa.Column("template_version", sa.String(128), nullable=False),
        sa.Column("snapshot", Json, nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", Json, nullable=True),
        sa.Column("images", Json, nullable=False),
        sa.Column("log_refs", Json, nullable=False),
        sa.Column("artifact_refs", Json, nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("started", sa.DateTime(), nullable=True),
        sa.Column("finished", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["stand_id", "project"], ["stand_instances.id", "stand_instances.project"],
            name="fk_stand_builds_instance"),
        sa.ForeignKeyConstraint(["project", "template_key", "template_version"],
            ["stand_templates.project", "stand_templates.key", "stand_templates.version"],
            name="fk_stand_builds_template"),
        sa.CheckConstraint("length(trim(id)) > 0", name="ck_stand_builds_id"),
        sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed', 'cancelled')", name="ck_stand_builds_status"),
    )
    for column in ("project", "stand_id", "job_id", "status"):
        op.create_index(f"ix_stand_builds_{column}", "stand_builds", [column])
    op.create_table(
        "stand_port_reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stand_id", sa.String(128), nullable=False),
        sa.Column("target_host", sa.String(128), nullable=False),
        sa.Column("protocol", sa.String(3), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("bind_address", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(["stand_id", "target_host"], ["stand_instances.id", "stand_instances.target_host"],
            name="fk_stand_ports_instance_host"),
        sa.UniqueConstraint("target_host", "protocol", "port", name="uq_stand_ports_host_protocol_port"),
        sa.CheckConstraint("protocol IN ('tcp', 'udp')", name="ck_stand_ports_protocol"),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name="ck_stand_ports_port"),
    )
    op.create_index("ix_stand_port_reservations_stand_id", "stand_port_reservations", ["stand_id"])


def downgrade() -> None:
    op.drop_table("stand_port_reservations")
    op.drop_table("stand_builds")
    op.drop_table("stand_instances")
    op.drop_table("stand_templates")
    op.drop_table("stand_repositories")
