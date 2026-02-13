"""Add sessions telegram_id index

Revision ID: 20260212_0003
Revises: 20260212_0002
Create Date: 2026-02-12 00:30:00

"""
from typing import Sequence, Union

from alembic import op


revision: str = '20260212_0003'
down_revision: Union[str, Sequence[str], None] = '20260212_0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_sessions_telegram_id', 'sessions', ['telegram_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_sessions_telegram_id', table_name='sessions')
