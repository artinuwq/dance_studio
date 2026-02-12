"""Add sessions table

Revision ID: 20260212_0002
Revises: 20260211_0001
Create Date: 2026-02-12 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '20260212_0002'
down_revision: Union[str, Sequence[str], None] = '20260211_0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sessions',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('telegram_id', sa.Integer(), nullable=False),
        sa.Column('user_agent_hash', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_sessions_expires_at', 'sessions', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_sessions_expires_at', table_name='sessions')
    op.drop_table('sessions')
