"""Keep deleted stands and build history while releasing active names."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy.dialects.postgresql import JSONB

    op.add_column("stand_templates", sa.Column("output_binding_titles", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True))
    op.add_column("stand_instances", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("stand_instances") as batch:
        batch.drop_constraint("uq_stand_instances_project_name", type_="unique")
        batch.create_index("uq_stand_instances_project_name", ["project", "name"], unique=True,
                           sqlite_where=sa.text("deleted_at IS NULL"), postgresql_where=sa.text("deleted_at IS NULL"))


def downgrade():
    # Reused active names cannot satisfy the older schema. Refuse rather than lose history.
    connection = op.get_bind()
    duplicate = connection.execute(sa.text("SELECT 1 FROM stand_instances GROUP BY project, name HAVING count(*) > 1")).first()
    if duplicate:
        raise RuntimeError("cannot downgrade while deleted stand names have been reused")
    with op.batch_alter_table("stand_instances") as batch:
        batch.drop_index("uq_stand_instances_project_name")
        batch.create_unique_constraint("uq_stand_instances_project_name", ["project", "name"])
        batch.drop_column("deleted_at")
    op.drop_column("stand_templates", "output_binding_titles")
