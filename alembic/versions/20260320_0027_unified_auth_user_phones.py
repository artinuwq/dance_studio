"""Add verified user phones and identity login metadata.

Revision ID: 20260320_0027_unified_auth
Revises: 20260316_0026_multi_platform
Create Date: 2026-03-20
"""

from alembic import op
import sqlalchemy as sa


revision = "20260320_0027_unified_auth"
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

    if "user_phones" not in tables:
        op.create_table(
            "user_phones",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("phone_e164", sa.String(length=32), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="sms"),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("phone_e164", name="uq_user_phones_phone_e164"),
        )
        op.create_index("ix_user_phones_user_id", "user_phones", ["user_id"])
        op.create_index("ix_user_phones_phone_e164", "user_phones", ["phone_e164"], unique=True)

    identity_cols = _column_names(bind, "auth_identities")
    if "linked_at" not in identity_cols:
        op.add_column("auth_identities", sa.Column("linked_at", sa.DateTime(), nullable=True))
        op.execute("UPDATE auth_identities SET linked_at = COALESCE(created_at, CURRENT_TIMESTAMP)")
        op.alter_column("auth_identities", "linked_at", nullable=False)
    if "last_login_at" not in identity_cols:
        op.add_column("auth_identities", sa.Column("last_login_at", sa.DateTime(), nullable=True))


def downgrade():
    bind = op.get_bind()
    tables = _table_names(bind)

    if "auth_identities" in tables:
        identity_cols = _column_names(bind, "auth_identities")
        if "last_login_at" in identity_cols:
            op.drop_column("auth_identities", "last_login_at")
        if "linked_at" in identity_cols:
            op.drop_column("auth_identities", "linked_at")

    if "user_phones" in tables:
        op.drop_index("ix_user_phones_phone_e164", table_name="user_phones")
        op.drop_index("ix_user_phones_user_id", table_name="user_phones")
        op.drop_table("user_phones")
