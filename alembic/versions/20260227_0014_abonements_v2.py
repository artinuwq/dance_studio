"""Add abonement types, bundle fields, and group booking pricing fields

Revision ID: 20260227_0014_abonements_v2
Revises: 20260226_0013_users_tg_nullable
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa


revision = "20260227_0014_abonements_v2"
down_revision = "20260226_0013_users_tg_nullable"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "group_abonements",
        sa.Column("abonement_type", sa.String(), nullable=False, server_default="multi"),
    )
    op.add_column("group_abonements", sa.Column("bundle_id", sa.String(length=36), nullable=True))
    op.add_column("group_abonements", sa.Column("bundle_size", sa.Integer(), nullable=True))

    op.execute("UPDATE group_abonements SET abonement_type='multi' WHERE abonement_type IS NULL")
    op.execute("UPDATE group_abonements SET bundle_size=1 WHERE bundle_size IS NULL")

    op.create_check_constraint(
        "ck_group_abonements_bundle_size_range",
        "group_abonements",
        "bundle_size IS NULL OR (bundle_size >= 1 AND bundle_size <= 3)",
    )
    op.create_index(
        "ix_group_abonements_user_abonement_type",
        "group_abonements",
        ["user_id", "abonement_type"],
        unique=False,
    )
    op.create_index("ix_group_abonements_bundle_id", "group_abonements", ["bundle_id"], unique=False)
    op.alter_column("group_abonements", "abonement_type", server_default=None)

    op.add_column("booking_requests", sa.Column("abonement_type", sa.String(), nullable=True))
    op.add_column("booking_requests", sa.Column("bundle_group_ids_json", sa.Text(), nullable=True))
    op.add_column("booking_requests", sa.Column("requested_amount", sa.Integer(), nullable=True))
    op.add_column(
        "booking_requests",
        sa.Column("requested_currency", sa.String(length=8), nullable=True, server_default="RUB"),
    )
    op.alter_column("booking_requests", "requested_currency", server_default=None)


def downgrade():
    op.drop_column("booking_requests", "requested_currency")
    op.drop_column("booking_requests", "requested_amount")
    op.drop_column("booking_requests", "bundle_group_ids_json")
    op.drop_column("booking_requests", "abonement_type")

    op.drop_index("ix_group_abonements_bundle_id", table_name="group_abonements")
    op.drop_index("ix_group_abonements_user_abonement_type", table_name="group_abonements")
    op.drop_constraint("ck_group_abonements_bundle_size_range", "group_abonements", type_="check")
    op.drop_column("group_abonements", "bundle_size")
    op.drop_column("group_abonements", "bundle_id")
    op.drop_column("group_abonements", "abonement_type")

