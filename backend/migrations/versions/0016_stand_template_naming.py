"""Keep portal naming preferences separate from portable templates."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stand_templates", sa.Column("name_ref_exclusions", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True))


def downgrade():
    op.drop_column("stand_templates", "name_ref_exclusions")
