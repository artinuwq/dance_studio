"""Add passkey challenges/auth audit tables and merge review columns.

Revision ID: 20260320_0029_passkey_merge_review
Revises: 20260320_0028_auth_phone_race
Create Date: 2026-03-20 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260320_0029_passkey_merge_review"
down_revision = "20260320_0028_auth_phone_race"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "passkey_challenges" not in tables:
        op.create_table(
            "passkey_challenges",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("challenge", sa.String(length=255), nullable=False, unique=True),
            sa.Column("flow_type", sa.String(length=32), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("session_user_id", sa.Integer(), nullable=True),
            sa.Column("rp_id", sa.String(length=255), nullable=False),
            sa.Column("origin", sa.String(length=255), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("credential_id", sa.String(length=512), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        )
        op.create_index("ix_passkey_challenges_user_id", "passkey_challenges", ["user_id"])
        op.create_index("ix_passkey_challenges_expires_at", "passkey_challenges", ["expires_at"])

    if "auth_audit_events" not in tables:
        op.create_table(
            "auth_audit_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        )
        op.create_index("ix_auth_audit_events_user_id", "auth_audit_events", ["user_id"])
        op.create_index("ix_auth_audit_events_event_type", "auth_audit_events", ["event_type"])

    columns = {col["name"] for col in inspector.get_columns("user_merge_events")}
    if "case_status" not in columns:
        op.add_column("user_merge_events", sa.Column("case_status", sa.String(length=32), nullable=False, server_default="resolved"))
    if "conflict_source" not in columns:
        op.add_column("user_merge_events", sa.Column("conflict_source", sa.String(length=64), nullable=True))
    if "reviewed_by" not in columns:
        op.add_column("user_merge_events", sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True))
    if "reviewed_at" not in columns:
        op.add_column("user_merge_events", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    if "review_result" not in columns:
        op.add_column("user_merge_events", sa.Column("review_result", sa.String(length=32), nullable=True))
    if "resolved_at" not in columns:
        op.add_column("user_merge_events", sa.Column("resolved_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "auth_audit_events" in tables:
        op.drop_index("ix_auth_audit_events_event_type", table_name="auth_audit_events")
        op.drop_index("ix_auth_audit_events_user_id", table_name="auth_audit_events")
        op.drop_table("auth_audit_events")
    if "passkey_challenges" in tables:
        op.drop_index("ix_passkey_challenges_expires_at", table_name="passkey_challenges")
        op.drop_index("ix_passkey_challenges_user_id", table_name="passkey_challenges")
        op.drop_table("passkey_challenges")

    columns = {col["name"] for col in inspector.get_columns("user_merge_events")}
    if "resolved_at" in columns:
        op.drop_column("user_merge_events", "resolved_at")
    if "review_result" in columns:
        op.drop_column("user_merge_events", "review_result")
    if "reviewed_at" in columns:
        op.drop_column("user_merge_events", "reviewed_at")
    if "reviewed_by" in columns:
        op.drop_column("user_merge_events", "reviewed_by")
    if "conflict_source" in columns:
        op.drop_column("user_merge_events", "conflict_source")
    if "case_status" in columns:
        op.drop_column("user_merge_events", "case_status")
