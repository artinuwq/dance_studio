"""Fix phone uniqueness/race support and manual merge flag.

Revision ID: 20260320_0028_auth_phone_race
Revises: 20260320_0027_unified_auth
Create Date: 2026-03-20
"""

from alembic import op
import sqlalchemy as sa


revision = "20260320_0028_auth_phone_race"
down_revision = "20260320_0027_unified_auth"
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
    user_cols = _column_names(bind, "users")
    if "requires_manual_merge" not in user_cols:
        op.add_column("users", sa.Column("requires_manual_merge", sa.Boolean(), nullable=False, server_default=sa.false()))

    indexes = _index_names(bind, "user_phones")
    if "ix_user_phones_phone_e164" not in indexes:
        op.create_index("ix_user_phones_phone_e164", "user_phones", ["phone_e164"], unique=False)

    with op.batch_alter_table("user_phones") as batch_op:
        try:
            batch_op.drop_constraint("uq_user_phones_phone_e164", type_="unique")
        except Exception:
            pass

    if "ix_user_phones_verified_phone_unique" not in indexes:
        op.create_index(
            "ix_user_phones_verified_phone_unique",
            "user_phones",
            ["phone_e164"],
            unique=True,
            postgresql_where=sa.text("verified_at IS NOT NULL"),
            sqlite_where=sa.text("verified_at IS NOT NULL"),
        )


def downgrade():
    bind = op.get_bind()
    indexes = _index_names(bind, "user_phones")
    if "ix_user_phones_verified_phone_unique" in indexes:
        op.drop_index("ix_user_phones_verified_phone_unique", table_name="user_phones")

    user_cols = _column_names(bind, "users")
    if "requires_manual_merge" in user_cols:
        op.drop_column("users", "requires_manual_merge")
