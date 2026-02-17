"""Add attendance intentions table for planned absences

Revision ID: 20260217_0008_abs_intent
Revises: 20260215_0007_telegram_bigint
Create Date: 2026-02-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260217_0008_abs_intent"
down_revision = "20260215_0007_telegram_bigint"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "attendance_intentions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("schedule.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="will_miss"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="user_web"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("schedule_id", "user_id", name="uq_attendance_intentions_schedule_user"),
    )
    op.create_index(
        "ix_attendance_intentions_schedule_id",
        "attendance_intentions",
        ["schedule_id"],
        unique=False,
    )
    op.create_index(
        "ix_attendance_intentions_user_id",
        "attendance_intentions",
        ["user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_attendance_intentions_user_id", table_name="attendance_intentions")
    op.drop_index("ix_attendance_intentions_schedule_id", table_name="attendance_intentions")
    op.drop_table("attendance_intentions")
