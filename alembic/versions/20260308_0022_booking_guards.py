"""Add booking guards for duplicates, capacity, and reservation expiry.

Revision ID: 20260308_0022_booking_guards
Revises: 20260308_0021_status_normalize
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0022_booking_guards"
down_revision = "20260308_0021_status_normalize"
branch_labels = None
depends_on = None


_BOOKING_OCCUPYING_STATUSES_SQL = "'created','waiting_payment','confirmed','attended','no_show'"


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(col.get("name")) for col in inspector.get_columns(table_name)}


def _index_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(idx.get("name")) for idx in inspector.get_indexes(table_name)}


def upgrade():
    bind = op.get_bind()
    booking_columns = _column_names(bind, "booking_requests")
    booking_indexes = _index_names(bind, "booking_requests")

    if "reserved_until" not in booking_columns:
        op.add_column("booking_requests", sa.Column("reserved_until", sa.DateTime(), nullable=True))

    booking_indexes = _index_names(bind, "booking_requests")
    if "ix_booking_requests_user_id" not in booking_indexes:
        op.create_index("ix_booking_requests_user_id", "booking_requests", ["user_id"], unique=False)
    if "ix_booking_requests_group_id" not in booking_indexes:
        op.create_index("ix_booking_requests_group_id", "booking_requests", ["group_id"], unique=False)
    if "ix_booking_requests_reserved_until" not in booking_indexes:
        op.create_index("ix_booking_requests_reserved_until", "booking_requests", ["reserved_until"], unique=False)

    op.execute(
        """
        UPDATE booking_requests
        SET reserved_until = COALESCE(created_at, NOW()) + INTERVAL '48 hours'
        WHERE status = 'waiting_payment'
          AND reserved_until IS NULL
        """
    )
    op.execute(
        """
        UPDATE booking_requests
        SET status = 'cancelled',
            reserved_until = NULL,
            status_updated_at = COALESCE(status_updated_at, NOW()),
            status_updated_by_name = COALESCE(status_updated_by_name, 'system: reservation expired')
        WHERE status = 'waiting_payment'
          AND reserved_until IS NOT NULL
          AND reserved_until <= NOW()
        """
    )

    op.execute(
        f"""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY user_id, object_type, date, time_from, time_to
                       ORDER BY COALESCE(created_at, NOW()) ASC, id ASC
                   ) AS rn
            FROM booking_requests
            WHERE object_type IN ('individual', 'rental')
              AND date IS NOT NULL
              AND time_from IS NOT NULL
              AND time_to IS NOT NULL
              AND status IN ({_BOOKING_OCCUPYING_STATUSES_SQL})
        )
        UPDATE booking_requests AS b
        SET status = 'cancelled',
            reserved_until = NULL,
            status_updated_at = COALESCE(b.status_updated_at, NOW()),
            status_updated_by_name = COALESCE(b.status_updated_by_name, 'system: duplicate cleanup')
        FROM ranked AS r
        WHERE b.id = r.id
          AND r.rn > 1
        """
    )

    op.execute(
        f"""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY user_id, group_id, COALESCE(group_start_date, DATE '1970-01-01')
                       ORDER BY COALESCE(created_at, NOW()) ASC, id ASC
                   ) AS rn
            FROM booking_requests
            WHERE object_type = 'group'
              AND group_id IS NOT NULL
              AND status IN ({_BOOKING_OCCUPYING_STATUSES_SQL})
        )
        UPDATE booking_requests AS b
        SET status = 'cancelled',
            reserved_until = NULL,
            status_updated_at = COALESCE(b.status_updated_at, NOW()),
            status_updated_by_name = COALESCE(b.status_updated_by_name, 'system: duplicate cleanup')
        FROM ranked AS r
        WHERE b.id = r.id
          AND r.rn > 1
        """
    )

    op.execute(
        f"""
        WITH ranked AS (
            SELECT b.id,
                   g.max_students,
                   ROW_NUMBER() OVER (
                       PARTITION BY b.group_id
                       ORDER BY
                           CASE b.status
                               WHEN 'confirmed' THEN 0
                               WHEN 'attended' THEN 1
                               WHEN 'no_show' THEN 2
                               WHEN 'waiting_payment' THEN 3
                               WHEN 'created' THEN 4
                               ELSE 5
                           END ASC,
                           COALESCE(b.created_at, NOW()) ASC,
                           b.id ASC
                   ) AS rn
            FROM booking_requests AS b
            JOIN groups AS g ON g.id = b.group_id
            WHERE b.object_type = 'group'
              AND b.group_id IS NOT NULL
              AND b.status IN ({_BOOKING_OCCUPYING_STATUSES_SQL})
        )
        UPDATE booking_requests AS b
        SET status = 'cancelled',
            reserved_until = NULL,
            status_updated_at = COALESCE(b.status_updated_at, NOW()),
            status_updated_by_name = COALESCE(b.status_updated_by_name, 'system: capacity cleanup')
        FROM ranked AS r
        WHERE b.id = r.id
          AND r.rn > r.max_students
        """
    )

    booking_indexes = _index_names(bind, "booking_requests")
    if "uq_booking_req_user_slot_active" not in booking_indexes:
        op.execute(
            f"""
            CREATE UNIQUE INDEX uq_booking_req_user_slot_active
            ON booking_requests (user_id, object_type, date, time_from, time_to)
            WHERE object_type IN ('individual', 'rental')
              AND date IS NOT NULL
              AND time_from IS NOT NULL
              AND time_to IS NOT NULL
              AND status IN ({_BOOKING_OCCUPYING_STATUSES_SQL})
            """
        )
    if "uq_booking_req_user_group_active" not in booking_indexes:
        op.execute(
            f"""
            CREATE UNIQUE INDEX uq_booking_req_user_group_active
            ON booking_requests (user_id, group_id, COALESCE(group_start_date, DATE '1970-01-01'))
            WHERE object_type = 'group'
              AND group_id IS NOT NULL
              AND status IN ({_BOOKING_OCCUPYING_STATUSES_SQL})
            """
        )


def downgrade():
    bind = op.get_bind()
    booking_indexes = _index_names(bind, "booking_requests")
    booking_columns = _column_names(bind, "booking_requests")

    if "uq_booking_req_user_group_active" in booking_indexes:
        op.execute("DROP INDEX uq_booking_req_user_group_active")
    if "uq_booking_req_user_slot_active" in booking_indexes:
        op.execute("DROP INDEX uq_booking_req_user_slot_active")

    with op.batch_alter_table("booking_requests") as batch:
        if "ix_booking_requests_reserved_until" in booking_indexes:
            batch.drop_index("ix_booking_requests_reserved_until")
        if "ix_booking_requests_group_id" in booking_indexes:
            batch.drop_index("ix_booking_requests_group_id")
        if "ix_booking_requests_user_id" in booking_indexes:
            batch.drop_index("ix_booking_requests_user_id")
        if "reserved_until" in booking_columns:
            batch.drop_column("reserved_until")
