"""Make users.telegram_id nullable

Revision ID: 20260226_0013_users_tg_nullable
Revises: 20260225_0012_app_settings
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20260226_0013_users_tg_nullable"
down_revision = "20260225_0012_app_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "users",
        "telegram_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        "users",
        "telegram_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )

