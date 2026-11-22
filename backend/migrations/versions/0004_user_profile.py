"""профиль пользователя: e-mail, Telegram, последний вход

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('email', sa.String(length=200), server_default='', nullable=False))
    op.add_column('users', sa.Column('telegram', sa.String(length=64), server_default='', nullable=False))
    op.add_column('users', sa.Column('last_login', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_login')
    op.drop_column('users', 'telegram')
    op.drop_column('users', 'email')
