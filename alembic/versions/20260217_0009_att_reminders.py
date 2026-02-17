"""Add attendance reminders table for Telegram DM reminder delivery

Revision ID: 20260217_0009_att_reminders
Revises: 20260217_0008_abs_intent
Create Date: 2026-02-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260217_0009_att_reminders"
down_revision = "20260217_0008_abs_intent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "attendance_reminders",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("schedule.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("send_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("send_error", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        sa.Column("response_action", sa.String(), nullable=True),
        sa.Column("button_closed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("schedule_id", "user_id", name="uq_attendance_reminders_schedule_user"),
    )
    op.create_index(
        "ix_attendance_reminders_schedule_id",
        "attendance_reminders",
        ["schedule_id"],
        unique=False,
    )
    op.create_index(
        "ix_attendance_reminders_user_id",
        "attendance_reminders",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_attendance_reminders_send_status",
        "attendance_reminders",
        ["send_status"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_attendance_reminders_send_status", table_name="attendance_reminders")
    op.drop_index("ix_attendance_reminders_user_id", table_name="attendance_reminders")
    op.drop_index("ix_attendance_reminders_schedule_id", table_name="attendance_reminders")
    op.drop_table("attendance_reminders")
