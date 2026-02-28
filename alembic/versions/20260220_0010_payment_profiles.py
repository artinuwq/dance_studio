"""Add payment profiles table for switchable admin payment details

Revision ID: 20260220_0010_payment_profiles
Revises: 20260217_0009_att_reminders
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa


revision = "20260220_0010_payment_profiles"
down_revision = "20260217_0009_att_reminders"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payment_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False, unique=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("slot in (1, 2)", name="ck_payment_profiles_slot_range"),
        sa.UniqueConstraint("slot", name="uq_payment_profiles_slot"),
    )

    op.execute(
        """
        INSERT INTO payment_profiles (slot, title, details, is_active, created_at, updated_at)
        VALUES
            (1, 'Основные реквизиты', '', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            (2, 'Резервные реквизиты', '', FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )


def downgrade():
    op.drop_table("payment_profiles")
