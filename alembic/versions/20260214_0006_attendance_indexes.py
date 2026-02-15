
"""
add indexes for attendance
"""
from alembic import op
import sqlalchemy as sa

revision = '20260214_0006_attendance_indexes'
down_revision = '20260213_0005'
branch_labels = None
depends_on = None


def upgrade():
    # Unique attendance per schedule/user
    op.create_unique_constraint(
        'uq_attendance_schedule_user',
        'attendance',
        ['schedule_id', 'user_id']
    )
    # Index for abonement lookups
    op.create_index('ix_attendance_abonement_id', 'attendance', ['abonement_id'], unique=False)


def downgrade():
    op.drop_index('ix_attendance_abonement_id', table_name='attendance')
    op.drop_constraint('uq_attendance_schedule_user', 'attendance', type_='unique')
