"""Add user_discounts table

Revision ID: 20260302_0015_user_discounts
Revises: 20260227_0014_abonements_v2
Create Date: 2026-03-02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260302_0015_user_discounts"
down_revision = "20260227_0014_abonements_v2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_discounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("discount_type", sa.String(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("is_one_time", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade():
    op.drop_table("user_discounts")
