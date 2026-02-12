"""Add sid_hash to sessions

Revision ID: 20260212_0004
Revises: 20260212_0003
Create Date: 2026-02-12 01:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260212_0004'
down_revision: Union[str, Sequence[str], None] = '20260212_0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('sid_hash', sa.String(length=64), nullable=True))
    op.execute('DELETE FROM sessions')
    op.alter_column('sessions', 'sid_hash', nullable=False)
    op.create_unique_constraint('uq_sessions_sid_hash', 'sessions', ['sid_hash'])


def downgrade() -> None:
    op.drop_constraint('uq_sessions_sid_hash', 'sessions', type_='unique')
    op.drop_column('sessions', 'sid_hash')
