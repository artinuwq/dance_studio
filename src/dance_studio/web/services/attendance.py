from __future__ import annotations

from datetime import datetime

from dance_studio.core.time import utcnow

from sqlalchemy import or_

from dance_studio.core.statuses import ABONEMENT_STATUS_ACTIVE
from dance_studio.core.system_settings_service import get_setting_value
from dance_studio.db.models import (
    Attendance,
    AttendanceIntention,
    GroupAbonement,
    GroupAbonementActionLog,
    IndividualLesson,
    Schedule,
    Staff,
    User,
)
from dance_studio.web.constants import (
    ATTENDANCE_DEBIT_STATUSES,
    ATTENDANCE_INTENTION_LOCK_DELTA,
    ATTENDANCE_INTENTION_LOCKED_MESSAGE,
    ATTENDANCE_INTENTION_STATUS_WILL_MISS,
)

def _attendance_already_debited(db, attendance_id: int) -> bool:
    if not attendance_id:
        return False
    exists = db.query(GroupAbonementActionLog.id).filter_by(attendance_id=attendance_id).first()
    return bool(exists)

def _debit_abonement_for_attendance(db, attendance: Attendance, staff: Staff | None):
    if attendance.status not in ATTENDANCE_DEBIT_STATUSES:
        return False
    if _attendance_already_debited(db, attendance.id):
        return True
    if not attendance.abonement_id:
        return False
    abon = db.query(GroupAbonement).filter_by(id=attendance.abonement_id).first()
    if not abon or abon.balance_credits is None or abon.balance_credits <= 0:
        return False

    if getattr(attendance, "teacher_payout_rub", None) is None:
        raw_price = getattr(abon, "price_per_lesson_rub", None)
        try:
            lesson_price = int(raw_price)
        except (TypeError, ValueError):
            lesson_price = 0
        if lesson_price < 0:
            lesson_price = 0

        try:
            percent = int(get_setting_value(db, "teachers.payout_percent"))
        except Exception:
            percent = 40
        if percent < 0:
            percent = 0
        if percent > 100:
            percent = 100

        payout = (lesson_price * percent) // 100 if lesson_price and percent else 0
        attendance.lesson_price_rub = lesson_price
        attendance.teacher_percent = percent
        attendance.teacher_payout_rub = payout

    abon.balance_credits -= 1
    log = GroupAbonementActionLog(
        abonement_id=abon.id,
        action_type="debit_attendance",
        credits_delta=-1,
        attendance_id=attendance.id,
        actor_type="staff",
        actor_id=staff.id if staff else None,
    )
    db.add(log)
    return True

def _can_edit_schedule_attendance(db, schedule: Schedule) -> bool:
    window = _attendance_marking_window_info(schedule)
    return bool(window["is_open"])

def _load_group_roster(db, schedule: Schedule):
    group_id = _schedule_group_id(schedule)
    if not group_id:
        return []
    date_val = schedule.date
    abonements = db.query(GroupAbonement).filter(
        GroupAbonement.group_id == group_id,
        GroupAbonement.status == ABONEMENT_STATUS_ACTIVE,
    )
    if date_val:
        abonements = abonements.filter(
            or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= date_val),
            or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= date_val),
        )
    abonements = abonements.order_by(GroupAbonement.valid_to.is_(None), GroupAbonement.valid_to).all()
    roster = []
    seen = set()
    for abon in abonements:
        if abon.user_id in seen:
            continue
        seen.add(abon.user_id)
        user = db.query(User).filter_by(id=abon.user_id).first()
        if not user:
            continue
        roster.append({"user": user, "abonement": abon})
    return roster

def _schedule_group_id(schedule: Schedule) -> int | None:
    if schedule.group_id:
        return schedule.group_id
    if schedule.object_type == "group" and schedule.object_id:
        return schedule.object_id
    return None

def _resolve_group_active_abonement(db, user_id: int, group_id: int, date_val):
    if not group_id:
        return None
    query = db.query(GroupAbonement).filter(
        GroupAbonement.user_id == user_id,
        GroupAbonement.group_id == group_id,
        GroupAbonement.status == ABONEMENT_STATUS_ACTIVE,
    )
    if date_val:
        query = query.filter(
            or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= date_val),
            or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= date_val),
        )
    return query.order_by(GroupAbonement.valid_to.is_(None), GroupAbonement.valid_to).first()


def _can_user_set_absence_for_schedule(db, user: User, schedule: Schedule) -> bool:
    if schedule.status in {"cancelled", "deleted"}:
        return False

    if schedule.object_type == "group":
        group_id = _schedule_group_id(schedule)
        if not group_id:
            return False
        abon = _resolve_group_active_abonement(db, user.id, group_id, schedule.date)
        return bool(abon)

    if schedule.object_type == "individual":
        if not schedule.object_id:
            return False
        lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first()
        return bool(lesson and lesson.student_id == user.id)

    return False

def _schedule_start_datetime(schedule: Schedule) -> datetime | None:
    if not schedule.date:
        return None
    start_time = schedule.time_from or schedule.start_time
    if not start_time:
        return None
    return datetime.combine(schedule.date, start_time)

def _attendance_intention_lock_info(schedule: Schedule) -> dict:
    start_at = _schedule_start_datetime(schedule)
    if not start_at:
        return {
            "is_locked": False,
            "cutoff_at": None,
            "starts_at": None,
            "lock_message": None,
        }
    cutoff_at = start_at - ATTENDANCE_INTENTION_LOCK_DELTA
    is_locked = datetime.now() >= cutoff_at
    return {
        "is_locked": is_locked,
        "cutoff_at": cutoff_at.isoformat(),
        "starts_at": start_at.isoformat(),
        "lock_message": ATTENDANCE_INTENTION_LOCKED_MESSAGE if is_locked else None,
    }

def _attendance_marking_window_info(schedule: Schedule) -> dict:
    start_at = _schedule_start_datetime(schedule)
    if not start_at:
        return {
            "is_open": False,
            "phase": "unknown",
            "starts_at": None,
            "ends_at": None,
            "message": "Время занятия не задано.",
        }
    # Keep attendance editing open from lesson start until end of the same day.
    ends_at = start_at.replace(hour=23, minute=59, second=59, microsecond=0)
    now = datetime.now()
    if now < start_at:
        return {
            "is_open": False,
            "phase": "before_start",
            "starts_at": start_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "message": "До начала занятия показывается предварительная отметка: кто придет и кто не придет.",
        }
    if now <= ends_at:
        return {
            "is_open": True,
            "phase": "marking_open",
            "starts_at": start_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "message": f"Можно отмечать фактическую посещаемость до конца дня ({ends_at.strftime('%d.%m.%Y %H:%M')}).",
        }
    return {
        "is_open": False,
        "phase": "marking_closed",
        "starts_at": start_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "message": f"Окно отметки закрыто. Напишите админу в случае чего-либо.",
    }


def _auto_finalize_attendance_from_intentions(db, schedule: Schedule, now_utc: datetime | None = None) -> int:
    """
    After marking window closes, auto-fill missing attendance statuses
    from intention state:
    - will_miss -> absent
    - otherwise -> present
    Returns number of rows finalized during this call.
    """
    if not schedule:
        return 0
    window = _attendance_marking_window_info(schedule)
    if window.get("phase") != "marking_closed":
        return 0

    now = now_utc or utcnow()
    intentions = {
        row.user_id: row
        for row in db.query(AttendanceIntention).filter_by(schedule_id=schedule.id).all()
    }
    existing_rows = db.query(Attendance).filter_by(schedule_id=schedule.id).all()
    existing_by_user = {row.user_id: row for row in existing_rows}

    candidate_user_ids = set(existing_by_user.keys())
    roster_abonements_by_user = {}

    if schedule.object_type == "group":
        for row in _load_group_roster(db, schedule):
            user = row.get("user")
            if not user:
                continue
            candidate_user_ids.add(user.id)
            abon = row.get("abonement")
            if abon and getattr(abon, "id", None):
                roster_abonements_by_user[user.id] = abon.id
    elif schedule.object_type == "individual":
        lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first() if schedule.object_id else None
        if lesson and lesson.student_id:
            candidate_user_ids.add(lesson.student_id)

    candidate_user_ids.update(intentions.keys())
    if not candidate_user_ids:
        return 0

    finalized_count = 0
    for user_id in candidate_user_ids:
        att = existing_by_user.get(user_id)
        if att and (att.status or "").strip():
            continue

        planned = intentions.get(user_id)
        inferred_status = (
            "absent"
            if planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS
            else "present"
        )

        if not att:
            att = Attendance(schedule_id=schedule.id, user_id=user_id)
            db.add(att)
            existing_by_user[user_id] = att

        att.status = inferred_status
        if not att.comment:
            att.comment = "Авто-отметка из состояния приду/не приду"
        att.marked_at = now
        att.marked_by_staff_id = None

        if schedule.object_type == "group" and not att.abonement_id:
            abonement_id = roster_abonements_by_user.get(user_id)
            if not abonement_id:
                group_id = _schedule_group_id(schedule)
                if group_id:
                    resolved_abonement = _resolve_group_active_abonement(db, user_id, group_id, schedule.date)
                    if resolved_abonement:
                        abonement_id = resolved_abonement.id
            if abonement_id:
                att.abonement_id = abonement_id

        db.flush()
        _debit_abonement_for_attendance(db, att, None)
        finalized_count += 1

    return finalized_count

def _serialize_attendance_intention(row: AttendanceIntention | None) -> dict:
    if not row:
        return {
            "has_intention": False,
            "status": None,
            "reason": None,
            "updated_at": None,
        }
    return {
        "has_intention": True,
        "status": row.status,
        "reason": row.reason,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

def _serialize_attendance_intention_with_lock(row: AttendanceIntention | None, lock_info: dict) -> dict:
    payload = _serialize_attendance_intention(row)
    payload.update(lock_info)
    if lock_info.get("is_locked"):
        payload["banner"] = ATTENDANCE_INTENTION_LOCKED_MESSAGE
    else:
        payload["banner"] = None
    return payload

__all__ = [
    "_attendance_already_debited",
    "_auto_finalize_attendance_from_intentions",
    "_attendance_intention_lock_info",
    "_attendance_marking_window_info",
    "_can_edit_schedule_attendance",
    "_can_user_set_absence_for_schedule",
    "_debit_abonement_for_attendance",
    "_load_group_roster",
    "_serialize_attendance_intention_with_lock",
]

