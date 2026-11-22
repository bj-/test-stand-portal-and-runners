"""Технические компоненты мастера хранятся отдельно от portable шаблона."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stand_templates", sa.Column("technical_components", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True))


def downgrade():
    op.drop_column("stand_templates", "technical_components")
