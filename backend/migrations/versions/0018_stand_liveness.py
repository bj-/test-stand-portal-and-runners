"""Allow stands with confirmed liveness and a failed smoke suite."""
from alembic import op


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_stand_instances_status"
STATUSES = "'created', 'waiting', 'building', 'deploying', 'checking', 'ready', 'failed', 'stopped'"


def _replace_constraint(statuses):
    with op.batch_alter_table("stand_instances") as batch:
        batch.drop_constraint(CONSTRAINT, type_="check")
        batch.create_check_constraint(CONSTRAINT, f"status IN ({statuses})")


def upgrade():
    _replace_constraint(STATUSES + ", 'limited'")


def downgrade():
    op.execute("UPDATE stand_instances SET status = 'failed' WHERE status = 'limited'")
    _replace_constraint(STATUSES)
