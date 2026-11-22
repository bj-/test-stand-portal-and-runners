"""Fingerprint of an idempotent managed stand request."""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stand_instances", sa.Column("request_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("stand_instances", "request_hash")
