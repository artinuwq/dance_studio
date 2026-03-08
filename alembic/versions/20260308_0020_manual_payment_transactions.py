"""Refactor payment transactions for manual payment journal

Revision ID: 20260308_0020_manual_payments
Revises: 20260308_0019_notify_logs
Create Date: 2026-03-08
"""

import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0020_manual_payments"
down_revision = "20260308_0019_notify_logs"
branch_labels = None
depends_on = None


def _normalize_manual_status(raw_status) -> str:
    normalized = str(raw_status or "").strip().lower()
    if normalized in {"rejected", "reject", "failed", "payment_failed", "cancelled", "canceled"}:
        return "rejected"
    return "confirmed"


def _parse_meta(meta_raw) -> dict:
    if not meta_raw:
        return {}
    try:
        parsed = json.loads(meta_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _detect_payment_link(meta_raw, description_raw, fallback_object_id: int) -> tuple[str, int]:
    meta = _parse_meta(meta_raw)

    booking_id = meta.get("booking_id")
    try:
        booking_id = int(booking_id)
    except (TypeError, ValueError):
        booking_id = None

    if booking_id and booking_id > 0:
        return "booking", booking_id

    abonement_id = meta.get("abonement_id")
    try:
        abonement_id = int(abonement_id)
    except (TypeError, ValueError):
        abonement_id = None

    if abonement_id and abonement_id > 0:
        return "abonement", abonement_id

    description = str(description_raw or "").strip().lower()
    if "abonement" in description or "абонемент" in description:
        return "abonement", int(fallback_object_id)
    return "booking", int(fallback_object_id)


def upgrade():
    with op.batch_alter_table("payment_transactions") as batch_op:
        batch_op.add_column(sa.Column("payment_type", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("object_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("confirmed_by_admin", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("confirmed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("comment", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_payment_transactions_confirmed_by_admin_staff",
            "staff",
            ["confirmed_by_admin"],
            ["id"],
        )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, status, meta, description, paid_at, created_at
            FROM payment_transactions
            """
        )
    ).mappings().all()

    for row in rows:
        payment_type, object_id = _detect_payment_link(
            row.get("meta"),
            row.get("description"),
            int(row["id"]),
        )
        bind.execute(
            sa.text(
                """
                UPDATE payment_transactions
                SET status = :status,
                    payment_type = :payment_type,
                    object_id = :object_id,
                    confirmed_at = :confirmed_at,
                    comment = :comment
                WHERE id = :id
                """
            ),
            {
                "id": int(row["id"]),
                "status": _normalize_manual_status(row.get("status")),
                "payment_type": payment_type,
                "object_id": int(object_id),
                "confirmed_at": row.get("paid_at") or row.get("created_at"),
                "comment": row.get("description"),
            },
        )

    with op.batch_alter_table("payment_transactions") as batch_op:
        batch_op.alter_column("status", existing_type=sa.String(), type_=sa.String(length=32), nullable=False)
        batch_op.alter_column("payment_type", existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column("object_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("currency")
        batch_op.drop_column("provider")
        batch_op.drop_column("description")
        batch_op.drop_column("meta")
        batch_op.drop_column("paid_at")
        batch_op.create_check_constraint(
            "ck_payment_transactions_status_manual",
            "status in ('confirmed', 'rejected')",
        )
        batch_op.create_check_constraint(
            "ck_payment_transactions_payment_type_manual",
            "payment_type in ('booking', 'abonement')",
        )
        batch_op.create_index("ix_payment_transactions_user_id", ["user_id"], unique=False)
        batch_op.create_index("ix_payment_transactions_object_id", ["object_id"], unique=False)
        batch_op.create_index("ix_payment_transactions_payment_type", ["payment_type"], unique=False)
        batch_op.create_index("ix_payment_transactions_status", ["status"], unique=False)


def downgrade():
    with op.batch_alter_table("payment_transactions") as batch_op:
        batch_op.drop_index("ix_payment_transactions_status")
        batch_op.drop_index("ix_payment_transactions_payment_type")
        batch_op.drop_index("ix_payment_transactions_object_id")
        batch_op.drop_index("ix_payment_transactions_user_id")
        batch_op.drop_constraint("ck_payment_transactions_payment_type_manual", type_="check")
        batch_op.drop_constraint("ck_payment_transactions_status_manual", type_="check")
        batch_op.add_column(sa.Column("paid_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("meta", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("description", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provider", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("currency", sa.String(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, status, payment_type, object_id, confirmed_at, comment
            FROM payment_transactions
            """
        )
    ).mappings().all()

    for row in rows:
        payment_type = str(row.get("payment_type") or "").strip().lower()
        object_id = int(row.get("object_id") or 0)
        if payment_type == "abonement":
            meta_payload = {"abonement_id": object_id}
        else:
            meta_payload = {"booking_id": object_id}

        status = "paid" if str(row.get("status") or "").strip().lower() == "confirmed" else "failed"
        bind.execute(
            sa.text(
                """
                UPDATE payment_transactions
                SET status = :status,
                    paid_at = :paid_at,
                    description = :description,
                    meta = :meta,
                    provider = :provider,
                    currency = :currency
                WHERE id = :id
                """
            ),
            {
                "id": int(row["id"]),
                "status": status,
                "paid_at": row.get("confirmed_at"),
                "description": row.get("comment"),
                "meta": json.dumps(meta_payload, ensure_ascii=False),
                "provider": "manual",
                "currency": "RUB",
            },
        )

    with op.batch_alter_table("payment_transactions") as batch_op:
        batch_op.alter_column("provider", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("currency", existing_type=sa.String(), nullable=False)
        batch_op.drop_constraint("fk_payment_transactions_confirmed_by_admin_staff", type_="foreignkey")
        batch_op.drop_column("comment")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("confirmed_by_admin")
        batch_op.drop_column("object_id")
        batch_op.drop_column("payment_type")
