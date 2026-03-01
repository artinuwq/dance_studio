from __future__ import annotations

from datetime import date, datetime, time

from dance_studio.db.models import (
    Attendance,
    AttendanceIntention,
    AttendanceReminder,
    Direction,
    Group,
    GroupAbonement,
    GroupAbonementActionLog,
    IndividualLesson,
    Schedule,
    TeacherTimeOff,
)
from dance_studio.web.constants import INACTIVE_SCHEDULE_STATUSES

def format_schedule(s):
    """Форматирует расписание с информацией об учителе"""
    teacher_info = {}
    if s.teacher_staff:
        teacher_info = {
            "id": s.teacher_staff.id,
            "name": s.teacher_staff.name,
            "photo": s.teacher_staff.photo_path
        }
    
    return {
        "id": s.id,
        "title": s.title,
        "teacher_id": s.teacher_id,
        "teacher": teacher_info,
        "date": s.date.isoformat(),
        "start": str(s.start_time),
        "end": str(s.end_time)
    }

def format_schedule_v2(s):
    return {
        "id": s.id,
        "object_type": s.object_type,
        "object_id": s.object_id,
        "group_id": s.group_id,
        "teacher_id": s.teacher_id,
        "date": s.date.isoformat() if s.date else None,
        "time_from": str(s.time_from) if s.time_from else None,
        "time_to": str(s.time_to) if s.time_to else None,
        "status": s.status,
        "status_comment": s.status_comment,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "updated_by": s.updated_by
    }

def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute

def _minutes_to_time_str(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"

def _subtract_busy_intervals(start: int, end: int, busy_intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    current = start
    for busy_start, busy_end in busy_intervals:
        if busy_end <= current:
            continue
        if busy_start >= end:
            break
        if busy_start > current:
            segments.append((current, min(busy_start, end)))
        current = max(current, busy_end)
        if current >= end:
            break
    if current < end:
        segments.append((current, end))
    return segments

def _has_slot_conflict(start_min: int, duration_minutes: int, busy_intervals: list[tuple[int, int]]) -> bool:
    end_min = start_min + duration_minutes
    for busy_start, busy_end in busy_intervals:
        if start_min < busy_end and busy_start < end_min:
            return True
    return False

def _collect_busy_intervals(db, teacher_id: int, target_date: date) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []

    schedule_items = (
        db.query(Schedule)
        .filter(
            Schedule.teacher_id == teacher_id,
            Schedule.date == target_date,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .all()
    )
    for item in schedule_items:
        start = item.time_from or item.start_time
        end = item.time_to or item.end_time
        if not start or not end:
            continue
        start_min = _time_to_minutes(start)
        end_min = _time_to_minutes(end)
        if end_min <= start_min:
            continue
        intervals.append((start_min, end_min))

    lessons = db.query(IndividualLesson).filter_by(teacher_id=teacher_id, date=target_date).all()
    for lesson in lessons:
        if not lesson.time_from or not lesson.time_to:
            continue
        start_min = _time_to_minutes(lesson.time_from)
        end_min = _time_to_minutes(lesson.time_to)
        if end_min <= start_min:
            continue
        intervals.append((start_min, end_min))

    time_off_items = (
        db.query(TeacherTimeOff)
        .filter_by(teacher_id=teacher_id, date=target_date, status="active")
        .all()
    )
    for off in time_off_items:
        if off.time_from and off.time_to:
            start_min = _time_to_minutes(off.time_from)
            end_min = _time_to_minutes(off.time_to)
            if end_min > start_min:
                intervals.append((start_min, end_min))
        else:
            intervals.append((0, 24 * 60))

    return intervals

def _parse_iso_date(value, field_name: str):
    if not value or not isinstance(value, str):
        raise ValueError(f"{field_name} обязателен и должен быть строкой формата YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} должен быть в формате YYYY-MM-DD") from exc

def _parse_user_id_for_merge(payload: dict, field_name: str) -> int:
    raw_value = payload.get(field_name)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value

def _merge_attendance_rows(db, source_user_id: int, target_user_id: int) -> dict:
    moved = 0
    merged = 0
    relinked_logs = 0

    rows = (
        db.query(Attendance)
        .filter(Attendance.user_id == source_user_id)
        .order_by(Attendance.id.asc())
        .all()
    )
    for source_row in rows:
        target_row = db.query(Attendance).filter(
            Attendance.schedule_id == source_row.schedule_id,
            Attendance.user_id == target_user_id,
        ).first()

        if not target_row:
            source_row.user_id = target_user_id
            moved += 1
            continue

        if source_row.marked_at and (not target_row.marked_at or source_row.marked_at > target_row.marked_at):
            target_row.marked_at = source_row.marked_at
            target_row.marked_by_staff_id = source_row.marked_by_staff_id

        if source_row.status and target_row.status != source_row.status:
            if target_row.status not in {"present", "late"} or source_row.status in {"present", "late"}:
                target_row.status = source_row.status

        if not target_row.abonement_id and source_row.abonement_id:
            target_row.abonement_id = source_row.abonement_id
        if not target_row.comment and source_row.comment:
            target_row.comment = source_row.comment

        relinked = (
            db.query(GroupAbonementActionLog)
            .filter(GroupAbonementActionLog.attendance_id == source_row.id)
            .update({GroupAbonementActionLog.attendance_id: target_row.id}, synchronize_session=False)
        )
        relinked_logs += int(relinked or 0)

        db.delete(source_row)
        merged += 1

    return {"moved": moved, "merged": merged, "relinked_logs": relinked_logs}

def _merge_attendance_intentions_rows(db, source_user_id: int, target_user_id: int) -> dict:
    moved = 0
    merged = 0

    rows = (
        db.query(AttendanceIntention)
        .filter(AttendanceIntention.user_id == source_user_id)
        .order_by(AttendanceIntention.id.asc())
        .all()
    )
    for source_row in rows:
        target_row = db.query(AttendanceIntention).filter(
            AttendanceIntention.schedule_id == source_row.schedule_id,
            AttendanceIntention.user_id == target_user_id,
        ).first()
        if not target_row:
            source_row.user_id = target_user_id
            moved += 1
            continue

        source_updated = source_row.updated_at or source_row.created_at
        target_updated = target_row.updated_at or target_row.created_at
        if source_updated and (not target_updated or source_updated > target_updated):
            target_row.status = source_row.status
            target_row.reason = source_row.reason
            target_row.source = source_row.source
            target_row.updated_at = source_row.updated_at
        elif not target_row.reason and source_row.reason:
            target_row.reason = source_row.reason

        db.delete(source_row)
        merged += 1

    return {"moved": moved, "merged": merged}

def _merge_attendance_reminders_rows(db, source_user_id: int, target_user_id: int) -> dict:
    moved = 0
    merged = 0

    rows = (
        db.query(AttendanceReminder)
        .filter(AttendanceReminder.user_id == source_user_id)
        .order_by(AttendanceReminder.id.asc())
        .all()
    )
    for source_row in rows:
        target_row = db.query(AttendanceReminder).filter(
            AttendanceReminder.schedule_id == source_row.schedule_id,
            AttendanceReminder.user_id == target_user_id,
        ).first()
        if not target_row:
            source_row.user_id = target_user_id
            moved += 1
            continue

        source_updated = source_row.updated_at or source_row.created_at
        target_updated = target_row.updated_at or target_row.created_at
        if source_updated and (not target_updated or source_updated > target_updated):
            target_row.send_status = source_row.send_status
            target_row.send_error = source_row.send_error
            target_row.attempted_at = source_row.attempted_at
            target_row.sent_at = source_row.sent_at
            target_row.telegram_chat_id = source_row.telegram_chat_id
            target_row.telegram_message_id = source_row.telegram_message_id
            target_row.responded_at = source_row.responded_at
            target_row.response_action = source_row.response_action
            target_row.button_closed_at = source_row.button_closed_at
            target_row.updated_at = source_row.updated_at
        elif not target_row.response_action and source_row.response_action:
            target_row.response_action = source_row.response_action
            target_row.responded_at = source_row.responded_at or target_row.responded_at

        db.delete(source_row)
        merged += 1

    return {"moved": moved, "merged": merged}

def _append_merge_note(current_value: str | None, note: str) -> str:
    existing = (current_value or "").strip()
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}\n\n{note}"

def _serialize_client_abonement_for_admin(db, abonement: GroupAbonement) -> dict:
    group = db.query(Group).filter_by(id=abonement.group_id).first()
    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first() if group else None
    lessons_per_week = int(group.lessons_per_week) if group and group.lessons_per_week else None
    return {
        "id": abonement.id,
        "group_id": abonement.group_id,
        "group_name": group.name if group else None,
        "direction_title": direction.title if direction else None,
        "lessons_per_week": lessons_per_week,
        "abonement_type": abonement.abonement_type,
        "bundle_id": abonement.bundle_id,
        "bundle_size": abonement.bundle_size,
        "balance_credits": abonement.balance_credits,
        "status": abonement.status,
        "valid_from": abonement.valid_from.isoformat() if abonement.valid_from else None,
        "valid_to": abonement.valid_to.isoformat() if abonement.valid_to else None,
    }

def _parse_month_start(value: str | None):
    if not value:
        now = datetime.now()
        return date(now.year, now.month, 1)
    try:
        dt = datetime.strptime(value, "%Y-%m")
        return date(dt.year, dt.month, 1)
    except ValueError as exc:
        raise ValueError("month должен быть в формате YYYY-MM") from exc

def _schedule_group_id(schedule: Schedule) -> int | None:
    if schedule.group_id:
        return schedule.group_id
    if schedule.object_type == "group" and schedule.object_id:
        return schedule.object_id
    return None

__all__ = [
    "_append_merge_note",
    "_collect_busy_intervals",
    "_has_slot_conflict",
    "_merge_attendance_intentions_rows",
    "_merge_attendance_reminders_rows",
    "_merge_attendance_rows",
    "_minutes_to_time_str",
    "_parse_iso_date",
    "_parse_month_start",
    "_parse_user_id_for_merge",
    "_schedule_group_id",
    "_serialize_client_abonement_for_admin",
    "_subtract_busy_intervals",
    "_time_to_minutes",
    "format_schedule",
    "format_schedule_v2",
]
