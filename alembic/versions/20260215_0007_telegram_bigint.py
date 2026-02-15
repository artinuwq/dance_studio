"""Resize telegram_id columns to BigInteger

Revision ID: 20260215_0007_telegram_bigint
Revises: 20260214_0006_attendance_indexes
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260215_0007_telegram_bigint"
down_revision = "20260214_0006_attendance_indexes"
branch_labels = None
depends_on = None


def _alter_to_bigint(table, column, nullable=True):
    op.alter_column(
        table,
        column,
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=nullable,
        postgresql_using=f"{column}::bigint",
    )


def upgrade():
    _alter_to_bigint("users", "telegram_id", nullable=False)
    _alter_to_bigint("sessions", "telegram_id", nullable=False)
    _alter_to_bigint("staff", "telegram_id", nullable=True)
    _alter_to_bigint("booking_requests", "user_telegram_id", nullable=True)


def downgrade():
    # revert to Integer (int4)
    op.alter_column(
        "booking_requests",
        "user_telegram_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="user_telegram_id::integer",
    )
    op.alter_column(
        "staff",
        "telegram_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="telegram_id::integer",
    )
    op.alter_column(
        "sessions",
        "telegram_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="telegram_id::integer",
    )
    op.alter_column(
        "users",
        "telegram_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="telegram_id::integer",
    )

