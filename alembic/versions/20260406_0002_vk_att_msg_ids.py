"""Add VK message identifiers to attendance reminders.

Revision ID: 20260406_0002_vk_att_msg_ids
Revises: 20260405_0001_baseline
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260406_0002_vk_att_msg_ids"
down_revision = "20260405_0001_baseline"
branch_labels = None
depends_on = None


def _has_column(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    return column_name in columns


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "attendance_reminders", "vk_peer_id"):
        op.add_column("attendance_reminders", sa.Column("vk_peer_id", sa.BigInteger(), nullable=True))
    if not _has_column(bind, "attendance_reminders", "vk_message_id"):
        op.add_column("attendance_reminders", sa.Column("vk_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "attendance_reminders", "vk_message_id"):
        op.drop_column("attendance_reminders", "vk_message_id")
    if _has_column(bind, "attendance_reminders", "vk_peer_id"):
        op.drop_column("attendance_reminders", "vk_peer_id")
