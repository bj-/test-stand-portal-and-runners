"""Git server connections and project repository links. Tokens are outside SQL."""

from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "git_servers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
    )
    if op.get_context().dialect.name == "sqlite":
        # SQLite can add a nullable inline reference without rebuilding the table.
        op.execute("ALTER TABLE stand_repositories ADD COLUMN git_server_id VARCHAR(36) "
                   "CONSTRAINT fk_stand_repositories_git_server REFERENCES git_servers (id)")
    else:
        op.add_column("stand_repositories", sa.Column("git_server_id", sa.String(36), nullable=True))
        op.create_foreign_key("fk_stand_repositories_git_server", "stand_repositories", "git_servers",
                              ["git_server_id"], ["id"])


def downgrade():
    if op.get_context().dialect.name != "sqlite":
        op.drop_constraint("fk_stand_repositories_git_server", "stand_repositories", type_="foreignkey")
    op.drop_column("stand_repositories", "git_server_id")
    op.drop_table("git_servers")
