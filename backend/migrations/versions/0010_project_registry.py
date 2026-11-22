"""Настройки проектов БД и явные окружения экземпляров стендов."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None
Json = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("projects", sa.Column("configuration", Json, nullable=True))
    op.add_column("stand_instances", sa.Column("environments", Json, nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("stand_instances", "environments")
    op.drop_column("projects", "configuration")
