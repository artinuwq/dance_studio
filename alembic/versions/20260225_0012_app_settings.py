"""Add app settings and app setting changes tables

Revision ID: 20260225_0012_app_settings
Revises: 20260220_0011_pay_fields
Create Date: 2026-02-25
"""

from alembic import op
import sqlalchemy as sa


revision = "20260225_0012_app_settings"
down_revision = "20260220_0011_pay_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("value_type", sa.String(length=32), nullable=False, server_default="string"),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by_staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("key", name="uq_app_settings_key"),
    )
    op.create_index("ix_app_settings_is_public", "app_settings", ["is_public"], unique=False)

    op.create_table(
        "app_setting_changes",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("setting_id", sa.Integer(), sa.ForeignKey("app_settings.id"), nullable=False),
        sa.Column("setting_key", sa.String(length=128), nullable=False),
        sa.Column("old_value_json", sa.Text(), nullable=True),
        sa.Column("new_value_json", sa.Text(), nullable=False),
        sa.Column("changed_by_staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="api"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_app_setting_changes_setting_id", "app_setting_changes", ["setting_id"], unique=False)
    op.create_index("ix_app_setting_changes_created_at", "app_setting_changes", ["created_at"], unique=False)
    op.create_index("ix_app_setting_changes_setting_key", "app_setting_changes", ["setting_key"], unique=False)

    op.execute(
        """
        INSERT INTO app_settings (key, value_json, value_type, description, is_public, created_at, updated_at)
        VALUES
            ('contacts.admin_username', '"@admin_username"', 'string', 'Telegram username of admin account for user contact.', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('contacts.bot_username', '"@bot_username"', 'string', 'Telegram username of the studio bot account.', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('rental.base_hour_price_rub', '2500', 'int', 'Base hall rental price per hour in RUB.', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('rental.min_duration_minutes', '60', 'int', 'Minimum allowed rental duration in minutes.', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('rental.step_minutes', '30', 'int', 'Rental selection step in minutes.', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('rental.open_hour_local', '8', 'int', 'Hall opening hour (local time, 0-23).', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('rental.close_hour_local', '22', 'int', 'Hall closing hour (local time, 1-24).', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
            ('rental.require_admin_approval', 'true', 'bool', 'If true, rentals should be approved by admin workflow.', FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )

    op.alter_column("app_settings", "value_type", server_default=None)
    op.alter_column("app_settings", "is_public", server_default=None)
    op.alter_column("app_setting_changes", "source", server_default=None)


def downgrade():
    op.drop_index("ix_app_setting_changes_setting_key", table_name="app_setting_changes")
    op.drop_index("ix_app_setting_changes_created_at", table_name="app_setting_changes")
    op.drop_index("ix_app_setting_changes_setting_id", table_name="app_setting_changes")
    op.drop_table("app_setting_changes")

    op.drop_index("ix_app_settings_is_public", table_name="app_settings")
    op.drop_table("app_settings")
