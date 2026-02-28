"""Add fixed payment fields to payment profiles

Revision ID: 20260220_0011_pay_fields
Revises: 20260220_0010_payment_profiles
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa


revision = "20260220_0011_pay_fields"
down_revision = "20260220_0010_payment_profiles"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "payment_profiles",
        sa.Column("recipient_bank", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "payment_profiles",
        sa.Column("recipient_number", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "payment_profiles",
        sa.Column("recipient_full_name", sa.String(), nullable=False, server_default=""),
    )
    op.alter_column("payment_profiles", "recipient_bank", server_default=None)
    op.alter_column("payment_profiles", "recipient_number", server_default=None)
    op.alter_column("payment_profiles", "recipient_full_name", server_default=None)


def downgrade():
    op.drop_column("payment_profiles", "recipient_full_name")
    op.drop_column("payment_profiles", "recipient_number")
    op.drop_column("payment_profiles", "recipient_bank")
