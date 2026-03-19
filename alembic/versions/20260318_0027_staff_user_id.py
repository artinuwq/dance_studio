"""Add staff.user_id reference to users and backfill via auth_identities.

Revision ID: 20260318_0027_staff_user_id
Revises: 20260316_0026_multi_platform
Create Date: 2026-03-18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260318_0027_staff_user_id"
down_revision = "20260316_0026_multi_platform"
branch_labels = None
depends_on = None


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(col.get("name")) for col in inspector.get_columns(table_name)}


def _table_names(bind) -> set[str]:
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def upgrade():
    bind = op.get_bind()
    tables = _table_names(bind)

    if "staff" not in tables:
        return

    staff_cols = _column_names(bind, "staff")
    if "user_id" not in staff_cols:
        op.add_column("staff", sa.Column("user_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_staff_user_id", "staff", "users", ["user_id"], ["id"])
        op.create_index("ix_staff_user_id", "staff", ["user_id"])

    if "auth_identities" in tables and "users" in tables:
        op.execute(
            sa.text(
                """
                INSERT INTO auth_identities (
                    user_id,
                    provider,
                    provider_user_id,
                    provider_username,
                    provider_payload_json,
                    is_primary,
                    is_verified,
                    created_at,
                    updated_at
                )
                SELECT
                    users.id,
                    'telegram',
                    CAST(users.telegram_id AS TEXT),
                    users.username,
                    NULL,
                    TRUE,
                    TRUE,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                FROM users
                WHERE users.telegram_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM auth_identities ai
                    WHERE ai.provider = 'telegram'
                      AND ai.provider_user_id = CAST(users.telegram_id AS TEXT)
                  )
                """
            )
        )

        op.execute(
            sa.text(
                """
                UPDATE staff
                SET user_id = ai.user_id
                FROM auth_identities ai
                WHERE staff.user_id IS NULL
                  AND staff.telegram_id IS NOT NULL
                  AND ai.provider = 'telegram'
                  AND ai.provider_user_id = CAST(staff.telegram_id AS TEXT)
                """
            )
        )


def downgrade():
    bind = op.get_bind()
    tables = _table_names(bind)

    if "staff" not in tables:
        return

    staff_cols = _column_names(bind, "staff")
    if "user_id" in staff_cols:
        op.drop_index("ix_staff_user_id", table_name="staff")
        op.drop_constraint("fk_staff_user_id", "staff", type_="foreignkey")
        op.drop_column("staff", "user_id")
