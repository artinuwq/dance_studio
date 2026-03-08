"""Add booking payment deadline alert marker.

Revision ID: 20260308_0023_booking_payment_deadline_alerts
Revises: 20260308_0022_booking_guards
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0023_booking_payment_deadline_alerts"
down_revision = "20260308_0022_booking_guards"
branch_labels = None
depends_on = None


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(col.get("name")) for col in inspector.get_columns(table_name)}


def _index_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(idx.get("name")) for idx in inspector.get_indexes(table_name)}


def upgrade():
    bind = op.get_bind()
    booking_columns = _column_names(bind, "booking_requests")
    if "payment_deadline_alert_sent_at" not in booking_columns:
        op.add_column(
            "booking_requests",
            sa.Column("payment_deadline_alert_sent_at", sa.DateTime(), nullable=True),
        )

    booking_indexes = _index_names(bind, "booking_requests")
    if "ix_booking_requests_payment_deadline_alert_sent_at" not in booking_indexes:
        op.create_index(
            "ix_booking_requests_payment_deadline_alert_sent_at",
            "booking_requests",
            ["payment_deadline_alert_sent_at"],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    booking_columns = _column_names(bind, "booking_requests")
    booking_indexes = _index_names(bind, "booking_requests")

    with op.batch_alter_table("booking_requests") as batch:
        if "ix_booking_requests_payment_deadline_alert_sent_at" in booking_indexes:
            batch.drop_index("ix_booking_requests_payment_deadline_alert_sent_at")
        if "payment_deadline_alert_sent_at" in booking_columns:
            batch.drop_column("payment_deadline_alert_sent_at")
