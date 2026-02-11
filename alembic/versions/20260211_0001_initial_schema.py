"""Initial schema

Revision ID: 20260211_0001
Revises: 
Create Date: 2026-02-11 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '20260211_0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('directions',
        sa.Column('direction_id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('direction_type', sa.String(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('base_price', sa.Integer()),
        sa.Column('status', sa.String()),
        sa.Column('is_popular', sa.Integer()),
        sa.Column('image_path', sa.String()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table('hall_rentals',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('creator_id', sa.Integer(), nullable=False),
        sa.Column('creator_type', sa.String(), nullable=False),
        sa.Column('date', sa.Date()),
        sa.Column('time_from', sa.Time()),
        sa.Column('time_to', sa.Time()),
        sa.Column('purpose', sa.String()),
        sa.Column('review_status', sa.String()),
        sa.Column('payment_status', sa.String()),
        sa.Column('activity_status', sa.String()),
        sa.Column('comment', sa.Text()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('start_time', sa.DateTime()),
        sa.Column('end_time', sa.DateTime()),
        sa.Column('status', sa.String()),
        sa.Column('duration_minutes', sa.Integer()),
    )
    op.create_table('news',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('photo_path', sa.String()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String()),
    )
    op.create_table('staff',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('phone', sa.String()),
        sa.Column('email', sa.String()),
        sa.Column('telegram_id', sa.Integer()),
        sa.Column('position', sa.String(), nullable=False),
        sa.Column('specialization', sa.String()),
        sa.Column('bio', sa.Text()),
        sa.Column('photo_path', sa.String()),
        sa.Column('teaches', sa.Integer()),
        sa.Column('status', sa.String()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table('users',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('telegram_id', sa.Integer(), nullable=False, unique=True),
        sa.Column('username', sa.String()),
        sa.Column('phone', sa.String()),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('email', sa.String()),
        sa.Column('birth_date', sa.Date()),
        sa.Column('photo_path', sa.String()),
        sa.Column('registered_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String()),
        sa.Column('user_notes', sa.Text()),
        sa.Column('staff_notes', sa.Text()),
        sa.UniqueConstraint('telegram_id'),
    )
    op.create_table('direction_upload_sessions',
        sa.Column('session_id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('admin_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=False),
        sa.Column('telegram_user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('direction_type', sa.String(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('base_price', sa.Integer()),
        sa.Column('image_path', sa.String()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('session_token', sa.String(), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('session_token'),
    )
    op.create_table('groups',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('direction_id', sa.Integer(), sa.ForeignKey('directions.direction_id'), nullable=False),
        sa.Column('teacher_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('age_group', sa.String(), nullable=False),
        sa.Column('max_students', sa.Integer(), nullable=False),
        sa.Column('duration_minutes', sa.Integer(), nullable=False),
        sa.Column('lessons_per_week', sa.Integer()),
        sa.Column('chat_id', sa.Integer()),
        sa.Column('chat_invite_link', sa.String()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table('mailings',
        sa.Column('mailing_id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('creator_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('purpose', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('target_type', sa.String(), nullable=False),
        sa.Column('target_id', sa.String()),
        sa.Column('mailing_type', sa.String(), nullable=False),
        sa.Column('sent_at', sa.DateTime()),
        sa.Column('scheduled_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table('payment_transactions',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('description', sa.String()),
        sa.Column('meta', sa.Text()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('paid_at', sa.DateTime()),
        sa.CheckConstraint('amount > 0', name='ck_payment_transactions_amount_positive'),
    )
    op.create_table('teacher_time_off',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('teacher_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('time_from', sa.Time()),
        sa.Column('time_to', sa.Time()),
        sa.Column('reason', sa.Text()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table('teacher_working_hours',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('teacher_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=False),
        sa.Column('weekday', sa.Integer(), nullable=False),
        sa.Column('time_from', sa.Time(), nullable=False),
        sa.Column('time_to', sa.Time(), nullable=False),
        sa.Column('valid_from', sa.Date()),
        sa.Column('valid_to', sa.Date()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table('booking_requests',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('user_telegram_id', sa.Integer()),
        sa.Column('user_name', sa.String()),
        sa.Column('user_username', sa.String()),
        sa.Column('object_type', sa.String(), nullable=False),
        sa.Column('date', sa.Date()),
        sa.Column('time_from', sa.Time()),
        sa.Column('time_to', sa.Time()),
        sa.Column('duration_minutes', sa.Integer()),
        sa.Column('comment', sa.Text()),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('groups.id')),
        sa.Column('lessons_count', sa.Integer()),
        sa.Column('group_start_date', sa.Date()),
        sa.Column('valid_until', sa.Date()),
        sa.Column('overlaps_json', sa.Text()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('status_updated_by_id', sa.Integer()),
        sa.Column('status_updated_by_username', sa.String()),
        sa.Column('status_updated_by_name', sa.String()),
        sa.Column('status_updated_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('teacher_id', sa.Integer(), sa.ForeignKey('staff.id')),
    )
    op.create_table('group_abonements',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('groups.id'), nullable=False),
        sa.Column('balance_credits', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('valid_from', sa.DateTime()),
        sa.Column('valid_to', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('balance_credits >= 0', name='ck_group_abonements_balance_credits_non_negative'),
    )
    op.create_table('schedule',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('object_id', sa.Integer()),
        sa.Column('object_type', sa.String()),
        sa.Column('date', sa.Date()),
        sa.Column('time_from', sa.Time()),
        sa.Column('time_to', sa.Time()),
        sa.Column('status', sa.String()),
        sa.Column('status_comment', sa.Text()),
        sa.Column('updated_at', sa.DateTime()),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('staff.id')),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('groups.id')),
        sa.Column('teacher_id', sa.Integer(), sa.ForeignKey('staff.id')),
        sa.Column('title', sa.String()),
        sa.Column('start_time', sa.Time()),
        sa.Column('end_time', sa.Time()),
    )
    op.create_table('attendance',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('schedule_id', sa.Integer(), sa.ForeignKey('schedule.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('abonement_id', sa.Integer(), sa.ForeignKey('group_abonements.id')),
        sa.Column('marked_at', sa.DateTime()),
        sa.Column('marked_by_staff_id', sa.Integer(), sa.ForeignKey('staff.id')),
        sa.Column('comment', sa.Text()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table('individual_lessons',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('teacher_id', sa.Integer(), sa.ForeignKey('staff.id'), nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('booking_id', sa.Integer(), sa.ForeignKey('booking_requests.id')),
        sa.Column('date', sa.Date()),
        sa.Column('time_from', sa.Time()),
        sa.Column('time_to', sa.Time()),
        sa.Column('teacher_comment', sa.Text()),
        sa.Column('student_comment', sa.Text()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('duration_minutes', sa.Integer()),
        sa.Column('comment', sa.Text()),
        sa.Column('person_comment', sa.Text()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('status_updated_at', sa.DateTime()),
        sa.Column('status_updated_by_id', sa.Integer(), sa.ForeignKey('staff.id')),
    )
    op.create_table('schedule_overrides',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('schedule_id', sa.Integer(), sa.ForeignKey('schedule.id'), nullable=False),
        sa.Column('override_type', sa.String(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table('group_abonement_action_logs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('abonement_id', sa.Integer(), sa.ForeignKey('group_abonements.id'), nullable=False),
        sa.Column('action_type', sa.String(), nullable=False),
        sa.Column('credits_delta', sa.Integer()),
        sa.Column('reason', sa.String()),
        sa.Column('note', sa.Text()),
        sa.Column('attendance_id', sa.Integer(), sa.ForeignKey('attendance.id')),
        sa.Column('payment_id', sa.Integer(), sa.ForeignKey('payment_transactions.id')),
        sa.Column('actor_type', sa.String(), nullable=False),
        sa.Column('actor_id', sa.Integer()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('payload', sa.Text()),
    )
    op.create_index('ix_teacher_time_off_teacher_date', 'teacher_time_off', ['teacher_id', 'date'], unique=False)
    op.create_index('ix_teacher_working_hours_teacher_validity', 'teacher_working_hours', ['teacher_id', 'valid_from', 'valid_to'], unique=False)
    op.create_index('ix_teacher_working_hours_teacher_weekday', 'teacher_working_hours', ['teacher_id', 'weekday'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_teacher_working_hours_teacher_validity', table_name='teacher_working_hours')
    op.drop_index('ix_teacher_working_hours_teacher_weekday', table_name='teacher_working_hours')
    op.drop_index('ix_teacher_time_off_teacher_date', table_name='teacher_time_off')
    op.drop_table('group_abonement_action_logs')
    op.drop_table('schedule_overrides')
    op.drop_table('individual_lessons')
    op.drop_table('attendance')
    op.drop_table('schedule')
    op.drop_table('group_abonements')
    op.drop_table('booking_requests')
    op.drop_table('teacher_working_hours')
    op.drop_table('teacher_time_off')
    op.drop_table('payment_transactions')
    op.drop_table('mailings')
    op.drop_table('groups')
    op.drop_table('direction_upload_sessions')
    op.drop_table('users')
    op.drop_table('staff')
    op.drop_table('news')
    op.drop_table('hall_rentals')
    op.drop_table('directions')
