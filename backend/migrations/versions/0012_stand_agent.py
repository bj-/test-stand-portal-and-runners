"""Host-bound stand agents and explicit deployed build identity."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("api_tokens", sa.Column("stand_host", sa.String(128), nullable=True))
    op.add_column("api_tokens", sa.Column("last_seen", sa.DateTime(), nullable=True))
    op.add_column("stand_instances", sa.Column("deployed_build_id", sa.String(128), nullable=True))


def downgrade():
    op.drop_column("stand_instances", "deployed_build_id")
    op.drop_column("api_tokens", "last_seen")
    op.drop_column("api_tokens", "stand_host")
