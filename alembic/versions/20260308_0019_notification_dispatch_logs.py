"""Add notification dispatch logs for deduplicated notifications

Revision ID: 20260308_0019_notify_logs
Revises: 20260307_0018_group_chat_bigint
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0019_notify_logs"
down_revision = "20260307_0018_group_chat_bigint"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_dispatch_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notification_key", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_ref", sa.String(length=128), nullable=False),
        sa.Column("recipient_type", sa.String(length=32), nullable=False),
        sa.Column("recipient_ref", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_notification_dispatch_logs_unique",
        "notification_dispatch_logs",
        ["notification_key", "entity_type", "entity_ref", "recipient_type", "recipient_ref"],
        unique=True,
    )
    op.create_index(
        "ix_notification_dispatch_logs_created_at",
        "notification_dispatch_logs",
        ["created_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_notification_dispatch_logs_created_at", table_name="notification_dispatch_logs")
    op.drop_index("ix_notification_dispatch_logs_unique", table_name="notification_dispatch_logs")
    op.drop_table("notification_dispatch_logs")
