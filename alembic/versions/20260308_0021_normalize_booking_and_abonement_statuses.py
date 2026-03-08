"""Normalize BookingRequest and GroupAbonement statuses

Revision ID: 20260308_0021_status_normalize
Revises: 20260308_0020_manual_payments
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0021_status_normalize"
down_revision = "20260308_0020_manual_payments"
branch_labels = None
depends_on = None


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(col.get("name")) for col in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    booking_columns = _column_names(bind, "booking_requests")
    abonement_columns = _column_names(bind, "group_abonements")

    if "status" in booking_columns:
        op.execute(
            """
            UPDATE booking_requests
            SET status = CASE
                WHEN status IS NULL OR TRIM(LOWER(status)) IN ('', 'new', 'created') THEN 'created'
                WHEN TRIM(LOWER(status)) IN ('approved', 'awaiting_payment', 'waiting_payment') THEN 'waiting_payment'
                WHEN TRIM(LOWER(status)) IN ('paid', 'confirmed') THEN 'confirmed'
                WHEN TRIM(LOWER(status)) = 'attended' THEN 'attended'
                WHEN TRIM(LOWER(status)) = 'no_show' THEN 'no_show'
                WHEN TRIM(LOWER(status)) IN ('cancelled', 'canceled', 'rejected', 'payment_failed') THEN 'cancelled'
                ELSE 'created'
            END
            """
        )

    if "paid" in booking_columns:
        op.execute(
            """
            UPDATE booking_requests
            SET status = 'confirmed'
            WHERE COALESCE(paid, 0) = 1
            """
        )

    if "confirmed" in booking_columns:
        op.execute(
            """
            UPDATE booking_requests
            SET status = 'confirmed'
            WHERE COALESCE(confirmed, 0) = 1
            """
        )

    if "status" in abonement_columns:
        op.execute(
            """
            UPDATE group_abonements
            SET status = CASE
                WHEN status IS NULL OR TRIM(LOWER(status)) IN ('', 'pending', 'new', 'created', 'pending_activation', 'pending_payment')
                    THEN 'pending_payment'
                WHEN TRIM(LOWER(status)) = 'active' THEN 'active'
                WHEN TRIM(LOWER(status)) IN ('expired', 'inactive') THEN 'expired'
                WHEN TRIM(LOWER(status)) IN ('cancelled', 'canceled', 'rejected', 'blocked') THEN 'cancelled'
                ELSE 'pending_payment'
            END
            """
        )

    if "active" in abonement_columns:
        op.execute(
            """
            UPDATE group_abonements
            SET status = CASE
                WHEN COALESCE(active, 0) = 1 THEN 'active'
                ELSE 'pending_payment'
            END
            """
        )

    with op.batch_alter_table("booking_requests") as batch:
        if "paid" in booking_columns:
            batch.drop_column("paid")
        if "confirmed" in booking_columns:
            batch.drop_column("confirmed")
        batch.alter_column(
            "status",
            existing_type=sa.String(),
            type_=sa.String(length=32),
            nullable=False,
            server_default="created",
        )
        batch.create_check_constraint(
            "ck_booking_requests_status_normalized",
            "status in ('created', 'waiting_payment', 'confirmed', 'cancelled', 'attended', 'no_show')",
        )
        batch.create_index("ix_booking_requests_status", ["status"], unique=False)
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )

    with op.batch_alter_table("group_abonements") as batch:
        if "active" in abonement_columns:
            batch.drop_column("active")
        batch.alter_column(
            "status",
            existing_type=sa.String(),
            type_=sa.String(length=32),
            nullable=False,
            server_default="pending_payment",
        )
        batch.create_check_constraint(
            "ck_group_abonements_status_normalized",
            "status in ('pending_payment', 'active', 'expired', 'cancelled')",
        )
        batch.create_index("ix_group_abonements_status", ["status"], unique=False)
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )


def downgrade():
    with op.batch_alter_table("group_abonements") as batch:
        batch.drop_index("ix_group_abonements_status")
        batch.drop_constraint("ck_group_abonements_status_normalized", type_="check")

    op.execute(
        """
        UPDATE group_abonements
        SET status = CASE
            WHEN TRIM(LOWER(status)) = 'pending_payment' THEN 'pending_activation'
            WHEN TRIM(LOWER(status)) = 'active' THEN 'active'
            WHEN TRIM(LOWER(status)) = 'expired' THEN 'expired'
            WHEN TRIM(LOWER(status)) = 'cancelled' THEN 'blocked'
            ELSE 'pending_activation'
        END
        """
    )

    with op.batch_alter_table("group_abonements") as batch:
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            type_=sa.String(),
            nullable=False,
        )

    with op.batch_alter_table("booking_requests") as batch:
        batch.drop_index("ix_booking_requests_status")
        batch.drop_constraint("ck_booking_requests_status_normalized", type_="check")

    op.execute(
        """
        UPDATE booking_requests
        SET status = CASE
            WHEN TRIM(LOWER(status)) = 'created' THEN 'NEW'
            WHEN TRIM(LOWER(status)) = 'waiting_payment' THEN 'AWAITING_PAYMENT'
            WHEN TRIM(LOWER(status)) IN ('confirmed', 'attended', 'no_show') THEN 'PAID'
            WHEN TRIM(LOWER(status)) = 'cancelled' THEN 'CANCELLED'
            ELSE 'NEW'
        END
        """
    )

    with op.batch_alter_table("booking_requests") as batch:
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            type_=sa.String(),
            nullable=False,
        )
