"""Discount snapshots on bookings and one-time discount consumption tracking.

Revision ID: 20260305_0017_discounts_v2
Revises: 20260305_0016_pay_profiles3
Create Date: 2026-03-05
"""

from alembic import op
import sqlalchemy as sa


revision = "20260305_0017_discounts_v2"
down_revision = "20260305_0016_pay_profiles3"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "sqlite"


def upgrade():
    if _is_sqlite():
        with op.batch_alter_table("booking_requests", recreate="always") as batch:
            batch.add_column(sa.Column("amount_before_discount", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("applied_discount_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("applied_discount_amount", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_booking_requests_applied_discount_id",
                "user_discounts",
                ["applied_discount_id"],
                ["id"],
            )
            batch.create_index(
                "ix_booking_requests_applied_discount_id",
                ["applied_discount_id"],
                unique=False,
            )
    else:
        op.add_column("booking_requests", sa.Column("amount_before_discount", sa.Integer(), nullable=True))
        op.add_column("booking_requests", sa.Column("applied_discount_id", sa.Integer(), nullable=True))
        op.add_column("booking_requests", sa.Column("applied_discount_amount", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_booking_requests_applied_discount_id",
            "booking_requests",
            "user_discounts",
            ["applied_discount_id"],
            ["id"],
        )
        op.create_index(
            "ix_booking_requests_applied_discount_id",
            "booking_requests",
            ["applied_discount_id"],
            unique=False,
        )

    op.execute(
        """
        UPDATE booking_requests
        SET amount_before_discount = requested_amount
        WHERE requested_amount IS NOT NULL
          AND amount_before_discount IS NULL
        """
    )

    if _is_sqlite():
        with op.batch_alter_table("user_discounts", recreate="always") as batch:
            batch.add_column(sa.Column("consumed_at", sa.DateTime(), nullable=True))
            batch.add_column(sa.Column("consumed_booking_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_user_discounts_consumed_booking_id",
                "booking_requests",
                ["consumed_booking_id"],
                ["id"],
            )
            batch.create_index(
                "ix_user_discounts_user_active_created",
                ["user_id", "is_active", "created_at"],
                unique=False,
            )
    else:
        op.add_column("user_discounts", sa.Column("consumed_at", sa.DateTime(), nullable=True))
        op.add_column("user_discounts", sa.Column("consumed_booking_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_user_discounts_consumed_booking_id",
            "user_discounts",
            "booking_requests",
            ["consumed_booking_id"],
            ["id"],
        )
        op.create_index(
            "ix_user_discounts_user_active_created",
            "user_discounts",
            ["user_id", "is_active", "created_at"],
            unique=False,
        )


def downgrade():
    if _is_sqlite():
        with op.batch_alter_table("user_discounts", recreate="always") as batch:
            batch.drop_index("ix_user_discounts_user_active_created")
            batch.drop_constraint("fk_user_discounts_consumed_booking_id", type_="foreignkey")
            batch.drop_column("consumed_booking_id")
            batch.drop_column("consumed_at")
    else:
        op.drop_index("ix_user_discounts_user_active_created", table_name="user_discounts")
        op.drop_constraint("fk_user_discounts_consumed_booking_id", "user_discounts", type_="foreignkey")
        op.drop_column("user_discounts", "consumed_booking_id")
        op.drop_column("user_discounts", "consumed_at")

    if _is_sqlite():
        with op.batch_alter_table("booking_requests", recreate="always") as batch:
            batch.drop_index("ix_booking_requests_applied_discount_id")
            batch.drop_constraint("fk_booking_requests_applied_discount_id", type_="foreignkey")
            batch.drop_column("applied_discount_amount")
            batch.drop_column("applied_discount_id")
            batch.drop_column("amount_before_discount")
    else:
        op.drop_index("ix_booking_requests_applied_discount_id", table_name="booking_requests")
        op.drop_constraint("fk_booking_requests_applied_discount_id", "booking_requests", type_="foreignkey")
        op.drop_column("booking_requests", "applied_discount_amount")
        op.drop_column("booking_requests", "applied_discount_id")
        op.drop_column("booking_requests", "amount_before_discount")
