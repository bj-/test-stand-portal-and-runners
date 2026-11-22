"""Register the original Git URL of a stand repository."""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stand_repositories", sa.Column("git_url", sa.String(2048), nullable=True))


def downgrade():
    op.drop_column("stand_repositories", "git_url")
