"""Версионируемые наборы данных, привязки и закрытые операции.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None
Json = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("jobs", sa.Column("parent_job_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("jobs") as batch:
        batch.create_foreign_key("fk_jobs_parent_job", "jobs", ["parent_job_id"], ["id"])
    op.create_index("ix_jobs_parent_job_id", "jobs", ["parent_job_id"])
    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project", sa.String(32), sa.ForeignKey("projects.code"), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("format", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project", "key", name="uq_datasets_project_key"),
    )
    op.create_index("ix_datasets_project", "datasets", ["project"])
    op.create_table(
        "dataset_versions",
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("data", Json, nullable=False),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "data_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project", sa.String(32), sa.ForeignKey("projects.code"), nullable=False),
        sa.Column("stand", sa.String(200), nullable=False),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_apply", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id", "version"], ["dataset_versions.dataset_id", "dataset_versions.version"],
                                name="fk_binding_version"),
        sa.UniqueConstraint("project", "stand", "target", "dataset_id", name="uq_binding_dataset_target"),
    )
    op.create_index("ix_data_bindings_project", "data_bindings", ["project"])
    op.create_table(
        "data_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("request", Json, nullable=False),
        sa.Column("adapter", Json, nullable=False),
        sa.Column("result", Json, nullable=True),
        sa.UniqueConstraint("job_id", "position", name="uq_data_operations_job_position"),
    )
    op.create_index("ix_data_operations_job_id", "data_operations", ["job_id"])


def downgrade() -> None:
    op.drop_table("data_operations")
    op.drop_table("data_bindings")
    op.drop_table("dataset_versions")
    op.drop_table("datasets")
    op.drop_index("ix_jobs_parent_job_id", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("fk_jobs_parent_job", type_="foreignkey")
        batch.drop_column("parent_job_id")
