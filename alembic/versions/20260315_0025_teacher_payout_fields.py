"""Add teacher payout and abonement pricing fields.

Revision ID: 20260315_0025_teacher_payout
Revises: 20260308_0024_client_photo_clean
Create Date: 2026-03-15
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260315_0025_teacher_payout"
down_revision = "20260308_0024_client_photo_clean"
branch_labels = None
depends_on = None


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {str(col.get("name")) for col in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()

    group_cols = _column_names(bind, "group_abonements")
    attendance_cols = _column_names(bind, "attendance")

    if "price_total_rub" not in group_cols:
        op.add_column("group_abonements", sa.Column("price_total_rub", sa.Integer(), nullable=True))
    if "lessons_total" not in group_cols:
        op.add_column("group_abonements", sa.Column("lessons_total", sa.Integer(), nullable=True))
    if "price_per_lesson_rub" not in group_cols:
        op.add_column("group_abonements", sa.Column("price_per_lesson_rub", sa.Integer(), nullable=True))

    if "lesson_price_rub" not in attendance_cols:
        op.add_column("attendance", sa.Column("lesson_price_rub", sa.Integer(), nullable=True))
    if "teacher_percent" not in attendance_cols:
        op.add_column("attendance", sa.Column("teacher_percent", sa.Integer(), nullable=True))
    if "teacher_payout_rub" not in attendance_cols:
        op.add_column("attendance", sa.Column("teacher_payout_rub", sa.Integer(), nullable=True))


def downgrade():
    bind = op.get_bind()

    group_cols = _column_names(bind, "group_abonements")
    attendance_cols = _column_names(bind, "attendance")

    if "teacher_payout_rub" in attendance_cols:
        op.drop_column("attendance", "teacher_payout_rub")
    if "teacher_percent" in attendance_cols:
        op.drop_column("attendance", "teacher_percent")
    if "lesson_price_rub" in attendance_cols:
        op.drop_column("attendance", "lesson_price_rub")

    if "price_per_lesson_rub" in group_cols:
        op.drop_column("group_abonements", "price_per_lesson_rub")
    if "lessons_total" in group_cols:
        op.drop_column("group_abonements", "lessons_total")
    if "price_total_rub" in group_cols:
        op.drop_column("group_abonements", "price_total_rub")
