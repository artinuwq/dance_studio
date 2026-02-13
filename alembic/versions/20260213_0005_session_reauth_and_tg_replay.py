"""Add session reauth flags and used init data replay table

Revision ID: 20260213_0005
Revises: 20260212_0004
Create Date: 2026-02-13 00:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260213_0005'
down_revision: Union[str, Sequence[str], None] = '20260212_0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('ip_prefix', sa.String(length=64), nullable=True))
    op.add_column('sessions', sa.Column('need_reauth', sa.Boolean(), nullable=True))
    op.add_column('sessions', sa.Column('reauth_reason', sa.String(length=255), nullable=True))
    op.add_column('sessions', sa.Column('last_seen', sa.DateTime(), nullable=True))

    op.execute('UPDATE sessions SET need_reauth = FALSE WHERE need_reauth IS NULL')
    op.execute('UPDATE sessions SET last_seen = created_at WHERE last_seen IS NULL')

    op.alter_column('sessions', 'need_reauth', nullable=False)
    op.alter_column('sessions', 'last_seen', nullable=False)
    op.create_index('ix_sessions_last_seen', 'sessions', ['last_seen'])

    op.create_table(
        'used_init_data',
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key_hash')
    )
    op.create_index('ix_used_init_data_expires_at', 'used_init_data', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_used_init_data_expires_at', table_name='used_init_data')
    op.drop_table('used_init_data')

    op.drop_index('ix_sessions_last_seen', table_name='sessions')
    op.drop_column('sessions', 'last_seen')
    op.drop_column('sessions', 'reauth_reason')
    op.drop_column('sessions', 'need_reauth')
    op.drop_column('sessions', 'ip_prefix')
