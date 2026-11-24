"""Store the UTC end of the requested expiration day for new stands."""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stand_instances", sa.Column("delete_after", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("stand_instances", "delete_after")
