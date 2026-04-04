"""Baseline schema snapshot for the current test/dev state.

Revision ID: 20260405_0001_baseline
Revises:
Create Date: 2026-04-05
"""

from alembic import op


revision = "20260405_0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from dance_studio.db.models import Base

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from dance_studio.db.models import Base

    Base.metadata.drop_all(bind=op.get_bind())
