"""Add multi-platform auth identities, notifications, and PWA support tables.

Revision ID: 20260316_0026_multi_platform
Revises: 20260315_0025_teacher_payout
Create Date: 2026-03-16
"""

from alembic import op
import sqlalchemy as sa


revision = "20260316_0026_multi_platform"
down_revision = "20260315_0025_teacher_payout"
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

    if "auth_identities" not in tables:
        op.create_table(
            "auth_identities",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_user_id", sa.String(length=255), nullable=True),
            sa.Column("provider_username", sa.String(length=255), nullable=True),
            sa.Column("provider_payload_json", sa.Text(), nullable=True),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("provider", "provider_user_id", name="uq_auth_identities_provider_user"),
        )
        op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])
        op.create_index("ix_auth_identities_provider", "auth_identities", ["provider"])

    if "phone_verification_codes" not in tables:
        op.create_table(
            "phone_verification_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("phone", sa.String(length=32), nullable=False),
            sa.Column("code_hash", sa.String(length=255), nullable=False),
            sa.Column("purpose", sa.String(length=32), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("delivery_channel", sa.String(length=32), nullable=False, server_default="none"),
            sa.Column("delivery_target", sa.String(length=255), nullable=True),
        )
        op.create_index("ix_phone_verification_codes_phone", "phone_verification_codes", ["phone"])
        op.create_index("ix_phone_verification_codes_expires_at", "phone_verification_codes", ["expires_at"])

    if "passkey_credentials" not in tables:
        op.create_table(
            "passkey_credentials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("credential_id", sa.String(length=512), nullable=False, unique=True),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("transports", sa.String(length=255), nullable=True),
            sa.Column("device_name", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_passkey_credentials_user_id", "passkey_credentials", ["user_id"])

    if "user_merge_events" not in tables:
        op.create_table(
            "user_merge_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("target_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("merge_reason", sa.String(length=64), nullable=False),
            sa.Column("merge_strategy", sa.String(length=64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_user_merge_events_source_user_id", "user_merge_events", ["source_user_id"])
        op.create_index("ix_user_merge_events_target_user_id", "user_merge_events", ["target_user_id"])

    if "notification_channels" not in tables:
        op.create_table(
            "notification_channels",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("channel_type", sa.String(length=32), nullable=False),
            sa.Column("target_ref", sa.String(length=512), nullable=False),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("channel_type", "target_ref", name="uq_notification_channels_target"),
        )
        op.create_index("ix_notification_channels_user_id", "notification_channels", ["user_id"])

    if "notification_preferences" not in tables:
        op.create_table(
            "notification_preferences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("channel_type", sa.String(length=32), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_notification_preferences_user_id", "notification_preferences", ["user_id"])
        op.create_index("ix_notification_preferences_event_type", "notification_preferences", ["event_type"])

    if "notifications" not in tables:
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(), nullable=True),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
        op.create_index("ix_notifications_status", "notifications", ["status"])

    if "notification_deliveries" not in tables:
        op.create_table(
            "notification_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("notification_id", sa.Integer(), sa.ForeignKey("notifications.id"), nullable=False),
            sa.Column("channel_type", sa.String(length=32), nullable=False),
            sa.Column("target_ref", sa.String(length=512), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("provider_message_id", sa.String(length=255), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("attempted_at", sa.DateTime(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=True),
        )
        op.create_index("ix_notification_deliveries_notification_id", "notification_deliveries", ["notification_id"])
        op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"])

    if "web_push_subscriptions" not in tables:
        op.create_table(
            "web_push_subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("endpoint", sa.String(length=1024), nullable=False, unique=True),
            sa.Column("p256dh", sa.Text(), nullable=False),
            sa.Column("auth", sa.Text(), nullable=False),
            sa.Column("user_agent", sa.String(length=512), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_web_push_subscriptions_user_id", "web_push_subscriptions", ["user_id"])

    user_cols = _column_names(bind, "users")
    if "primary_phone" not in user_cols:
        op.add_column("users", sa.Column("primary_phone", sa.String(), nullable=True))
    if "phone_verified_at" not in user_cols:
        op.add_column("users", sa.Column("phone_verified_at", sa.DateTime(), nullable=True))
    if "merged_to_user_id" not in user_cols:
        op.add_column("users", sa.Column("merged_to_user_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_users_merged_to_user_id", "users", "users", ["merged_to_user_id"], ["id"])
    if "is_archived" not in user_cols:
        op.add_column("users", sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "preferred_notification_channel" not in user_cols:
        op.add_column("users", sa.Column("preferred_notification_channel", sa.String(length=32), nullable=True))
    if "last_login_at" not in user_cols:
        op.add_column("users", sa.Column("last_login_at", sa.DateTime(), nullable=True))

    session_cols = _column_names(bind, "sessions")
    if "user_id" not in session_cols:
        op.add_column("sessions", sa.Column("user_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_sessions_user_id", "sessions", "users", ["user_id"], ["id"])
        op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    if "telegram_id" in session_cols:
        op.alter_column("sessions", "telegram_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade():
    bind = op.get_bind()
    tables = _table_names(bind)

    session_cols = _column_names(bind, "sessions")
    if "user_id" in session_cols:
        op.drop_index("ix_sessions_user_id", table_name="sessions")
        op.drop_constraint("fk_sessions_user_id", "sessions", type_="foreignkey")
        op.drop_column("sessions", "user_id")
    if "telegram_id" in session_cols:
        op.alter_column("sessions", "telegram_id", existing_type=sa.BigInteger(), nullable=False)

    user_cols = _column_names(bind, "users")
    if "last_login_at" in user_cols:
        op.drop_column("users", "last_login_at")
    if "preferred_notification_channel" in user_cols:
        op.drop_column("users", "preferred_notification_channel")
    if "is_archived" in user_cols:
        op.drop_column("users", "is_archived")
    if "merged_to_user_id" in user_cols:
        op.drop_constraint("fk_users_merged_to_user_id", "users", type_="foreignkey")
        op.drop_column("users", "merged_to_user_id")
    if "phone_verified_at" in user_cols:
        op.drop_column("users", "phone_verified_at")
    if "primary_phone" in user_cols:
        op.drop_column("users", "primary_phone")

    if "web_push_subscriptions" in tables:
        op.drop_index("ix_web_push_subscriptions_user_id", table_name="web_push_subscriptions")
        op.drop_table("web_push_subscriptions")
    if "notification_deliveries" in tables:
        op.drop_index("ix_notification_deliveries_status", table_name="notification_deliveries")
        op.drop_index("ix_notification_deliveries_notification_id", table_name="notification_deliveries")
        op.drop_table("notification_deliveries")
    if "notifications" in tables:
        op.drop_index("ix_notifications_status", table_name="notifications")
        op.drop_index("ix_notifications_user_id", table_name="notifications")
        op.drop_table("notifications")
    if "notification_preferences" in tables:
        op.drop_index("ix_notification_preferences_event_type", table_name="notification_preferences")
        op.drop_index("ix_notification_preferences_user_id", table_name="notification_preferences")
        op.drop_table("notification_preferences")
    if "notification_channels" in tables:
        op.drop_index("ix_notification_channels_user_id", table_name="notification_channels")
        op.drop_table("notification_channels")
    if "user_merge_events" in tables:
        op.drop_index("ix_user_merge_events_target_user_id", table_name="user_merge_events")
        op.drop_index("ix_user_merge_events_source_user_id", table_name="user_merge_events")
        op.drop_table("user_merge_events")
    if "passkey_credentials" in tables:
        op.drop_index("ix_passkey_credentials_user_id", table_name="passkey_credentials")
        op.drop_table("passkey_credentials")
    if "phone_verification_codes" in tables:
        op.drop_index("ix_phone_verification_codes_expires_at", table_name="phone_verification_codes")
        op.drop_index("ix_phone_verification_codes_phone", table_name="phone_verification_codes")
        op.drop_table("phone_verification_codes")
    if "auth_identities" in tables:
        op.drop_index("ix_auth_identities_provider", table_name="auth_identities")
        op.drop_index("ix_auth_identities_user_id", table_name="auth_identities")
        op.drop_table("auth_identities")
