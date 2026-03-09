"""Clear legacy users.photo_path for non-staff clients.

Revision ID: 20260308_0024_client_photo_clean
Revises: 20260308_0023_booking_pay_alert
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0024_client_photo_clean"
down_revision = "20260308_0023_booking_pay_alert"
branch_labels = None
depends_on = None


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(col.get("name")) for col in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    user_columns = _column_names(bind, "users")
    staff_columns = _column_names(bind, "staff")
    if "photo_path" not in user_columns or "telegram_id" not in user_columns:
        return
    if "telegram_id" not in staff_columns or "status" not in staff_columns:
        return

    op.execute(
        """
        UPDATE users AS u
        SET photo_path = NULL
        WHERE u.photo_path IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM staff AS s
              WHERE s.telegram_id = u.telegram_id
                AND s.status = 'active'
          )
        """
    )


def downgrade():
    # Data cleanup is not reversible.
    pass
