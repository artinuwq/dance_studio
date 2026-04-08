from datetime import datetime

from dance_studio.core.time import utcnow

from flask import Blueprint, g, request

from dance_studio.db.models import Attendance, AttendanceIntention, BookingRequest, Group, IndividualLesson, Schedule, User
from dance_studio.web.constants import (
    ATTENDANCE_ALLOWED_STATUSES,
    ATTENDANCE_DEBIT_STATUSES,
    ATTENDANCE_INTENTION_LOCKED_MESSAGE,
    ATTENDANCE_INTENTION_STATUS_WILL_MISS,
    INACTIVE_SCHEDULE_STATUSES,
)
from dance_studio.core.system_settings_service import get_setting_value
from dance_studio.web.services.access import _get_current_staff, get_current_user_from_request, require_permission
from dance_studio.web.services.attendance import (
    _attendance_already_debited,
    _attendance_intention_lock_info,
    _attendance_marking_window_info,
    _can_user_set_absence_for_schedule,
    _debit_abonement_for_attendance,
    _load_group_roster,
    _resolve_group_active_abonement,
    _serialize_attendance_intention_with_lock,
)
bp = Blueprint('attendance_routes', __name__)


def _is_teacher_of_schedule(db, schedule: Schedule, staff) -> bool:
    if not staff:
        return False

    if schedule.teacher_id and schedule.teacher_id == staff.id:
        return True

    if schedule.object_type == "group":
        group_id = schedule.group_id or schedule.object_id
        if not group_id:
            return False
        group = db.query(Group).filter_by(id=group_id).first()
        return bool(group and group.teacher_id == staff.id)

    if schedule.object_type == "individual" and schedule.object_id:
        lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first()
        return bool(lesson and lesson.teacher_id == staff.id)

    return False


def _attendance_view_permission_error(db, schedule: Schedule):
    if getattr(g, "telegram_id", None) is None:
        return {"error": "Требуется аутентификация"}, 401

    perm_error = require_permission("manage_schedule")
    if not perm_error:
        return None

    status_code = perm_error[1] if isinstance(perm_error, tuple) and len(perm_error) > 1 else None
    if status_code in (400, 401):
        return perm_error

    staff = _get_current_staff(db)
    if _is_teacher_of_schedule(db, schedule, staff):
        return None

    return perm_error


@bp.route("/api/attendance/<int:schedule_id>", methods=["GET"])
def get_attendance(schedule_id):
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    perm_error = _attendance_view_permission_error(db, schedule)
    if perm_error:
        return perm_error

    window = _attendance_marking_window_info(schedule)

    # Money/finance info (lesson price, teacher payout) is only visible to system_settings users.
    financial_allowed = require_permission("system_settings") is None
    payout_percent = None
    if financial_allowed:
        try:
            payout_percent = int(get_setting_value(db, "teachers.payout_percent"))
        except Exception:
            payout_percent = 40
        if payout_percent < 0:
            payout_percent = 0
        if payout_percent > 100:
            payout_percent = 100

    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    existing = {a.user_id: a for a in db.query(Attendance).filter_by(schedule_id=schedule_id).all()}
    intentions = {
        row.user_id: row
        for row in db.query(AttendanceIntention).filter_by(schedule_id=schedule_id).all()
    }
    items = []
    roster_source = None
    roster_user_ids = set()

    if schedule.object_type == "group":
        roster_source = "group"
        group_roster = _load_group_roster(db, schedule)
        roster_user_ids = {row["user"].id for row in group_roster if row.get("user") and row["user"].id}
        for row in group_roster:
            user = row["user"]
            abon = row.get("abonement")
            att = existing.pop(user.id, None)
            planned = intentions.pop(user.id, None)
            planned_status = "will_miss" if (planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS) else "will_come"
            entry = {
                "user_id": user.id,
                "name": user.name,
                "username": user.username,
                "phone": user.phone,
                "status": att.status if att else None,
                "comment": att.comment if att else None,
                "abonement_id": att.abonement_id if att else (abon.id if abon else None),
                "debited": _attendance_already_debited(db, att.id) if att else False,
                "planned_absence": bool(planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS),
                "planned_absence_reason": planned.reason if planned else None,
                "planned_status": planned_status,
                # For group schedules, "active abonement" means the user is in the roster built from
                # active abonements valid for this schedule date.
                "active_abonement": True,
            }

            if financial_allowed:
                status_norm = str(entry.get("status") or "").strip().lower()
                counted = status_norm in ATTENDANCE_DEBIT_STATUSES
                if counted:
                    lesson_price = _safe_int(getattr(att, "lesson_price_rub", None)) if att else None
                    if lesson_price is None and abon:
                        lesson_price = _safe_int(getattr(abon, "price_per_lesson_rub", None))
                    if lesson_price is None and att:
                        lesson_price = _safe_int(getattr(getattr(att, "abonement", None), "price_per_lesson_rub", None))
                    lesson_price = 0 if lesson_price is None else lesson_price
                    if lesson_price < 0:
                        lesson_price = 0

                    percent = _safe_int(getattr(att, "teacher_percent", None)) if att else None
                    if percent is None:
                        percent = payout_percent
                    if percent is None or percent < 0:
                        percent = 0
                    if percent > 100:
                        percent = 100

                    payout = _safe_int(getattr(att, "teacher_payout_rub", None)) if att else None
                    if payout is None:
                        payout = (lesson_price * percent) // 100 if lesson_price and percent else 0
                else:
                    lesson_price = 0
                    payout = 0
                    percent = None

                entry.update({
                    "counted": counted,
                    "price_rub": lesson_price,
                    "payout_rub": payout,
                    "percent": percent,
                })

            items.append(entry)
    elif schedule.object_type == "individual":
        roster_source = "individual"
        lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first() if schedule.object_id else None
        booking_price = 0
        if financial_allowed and lesson and getattr(lesson, "booking_id", None):
            booking = db.query(BookingRequest).filter_by(id=lesson.booking_id).first()
            raw_amount = getattr(booking, "requested_amount", None) if booking else None
            booking_price = _safe_int(raw_amount)
            if booking_price is None and booking:
                amount_before = _safe_int(getattr(booking, "amount_before_discount", None)) or 0
                discount_amount = _safe_int(getattr(booking, "applied_discount_amount", None)) or 0
                computed = amount_before - discount_amount
                booking_price = computed if computed > 0 else 0
            if booking_price is None or booking_price < 0:
                booking_price = 0
        if lesson and lesson.student_id:
            user = db.query(User).filter_by(id=lesson.student_id).first()
            if user:
                att = existing.pop(user.id, None)
                planned = intentions.pop(user.id, None)
                planned_status = "will_miss" if (planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS) else "will_come"
                entry = {
                    "user_id": user.id,
                    "name": user.name,
                    "username": user.username,
                    "phone": user.phone,
                    "status": att.status if att else None,
                    "comment": att.comment if att else None,
                    "abonement_id": att.abonement_id if att else None,
                    "debited": _attendance_already_debited(db, att.id) if att else False,
                    "planned_absence": bool(planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS),
                    "planned_absence_reason": planned.reason if planned else None,
                    "planned_status": planned_status,
                }

                if financial_allowed:
                    status_norm = str(entry.get("status") or "").strip().lower()
                    counted = status_norm in ATTENDANCE_DEBIT_STATUSES
                    if counted:
                        lesson_price = _safe_int(getattr(att, "lesson_price_rub", None)) if att else None
                        if lesson_price is None:
                            lesson_price = booking_price or 0
                        if lesson_price < 0:
                            lesson_price = 0

                        percent = _safe_int(getattr(att, "teacher_percent", None)) if att else None
                        if percent is None:
                            percent = payout_percent
                        if percent is None or percent < 0:
                            percent = 0
                        if percent > 100:
                            percent = 100

                        payout = _safe_int(getattr(att, "teacher_payout_rub", None)) if att else None
                        if payout is None:
                            payout = (lesson_price * percent) // 100 if lesson_price and percent else 0
                    else:
                        lesson_price = 0
                        payout = 0
                        percent = None

                    entry.update({
                        "counted": counted,
                        "price_rub": lesson_price,
                        "payout_rub": payout,
                        "percent": percent,
                    })

                items.append(entry)

    # add remaining manual/legacy attendance
    for att in existing.values():
        user = db.query(User).filter_by(id=att.user_id).first()
        planned = intentions.pop(att.user_id, None)
        planned_status = "will_miss" if (planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS) else "will_come"
        entry = {
            "user_id": att.user_id,
            "name": user.name if user else None,
            "username": user.username if user else None,
            "phone": user.phone if user else None,
            "status": att.status,
            "comment": att.comment,
            "abonement_id": att.abonement_id,
            "debited": _attendance_already_debited(db, att.id),
            "planned_absence": bool(planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS),
            "planned_absence_reason": planned.reason if planned else None,
            "planned_status": planned_status,
            "active_abonement": bool(schedule.object_type == "group" and att.user_id in roster_user_ids),
        }

        if financial_allowed:
            status_norm = str(entry.get("status") or "").strip().lower()
            counted = status_norm in ATTENDANCE_DEBIT_STATUSES
            if counted:
                lesson_price = _safe_int(getattr(att, "lesson_price_rub", None))
                if lesson_price is None and schedule.object_type == "group":
                    lesson_price = _safe_int(getattr(getattr(att, "abonement", None), "price_per_lesson_rub", None))
                    if lesson_price is None:
                        group_id = schedule.group_id or schedule.object_id
                        abon = _resolve_group_active_abonement(db, att.user_id, group_id, schedule.date) if group_id else None
                        lesson_price = _safe_int(getattr(abon, "price_per_lesson_rub", None)) if abon else None
                if lesson_price is None and schedule.object_type == "individual":
                    lesson_price = booking_price or 0

                lesson_price = 0 if lesson_price is None else lesson_price
                if lesson_price < 0:
                    lesson_price = 0

                percent = _safe_int(getattr(att, "teacher_percent", None))
                if percent is None:
                    percent = payout_percent
                if percent is None or percent < 0:
                    percent = 0
                if percent > 100:
                    percent = 100

                payout = _safe_int(getattr(att, "teacher_payout_rub", None))
                if payout is None:
                    payout = (lesson_price * percent) // 100 if lesson_price and percent else 0
            else:
                lesson_price = 0
                payout = 0
                percent = None

            entry.update({
                "counted": counted,
                "price_rub": lesson_price,
                "payout_rub": payout,
                "percent": percent,
            })

        items.append(entry)

    for planned in intentions.values():
        user = db.query(User).filter_by(id=planned.user_id).first()
        entry = {
            "user_id": planned.user_id,
            "name": user.name if user else None,
            "username": user.username if user else None,
            "phone": user.phone if user else None,
            "status": None,
            "comment": None,
            "abonement_id": None,
            "debited": False,
            "planned_absence": planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS,
            "planned_absence_reason": planned.reason,
            "planned_status": "will_miss",
            "active_abonement": bool(schedule.object_type == "group" and planned.user_id in roster_user_ids),
        }
        if financial_allowed:
            entry.update({
                "counted": False,
                "price_rub": 0,
                "payout_rub": 0,
                "percent": None,
            })
        items.append(entry)

    status_labels = {
        "present": "Присутствовал",
        "absent": "Отсутствовал",
        "late": "Опоздал",
        "sick": "Болел",
    }
    debit_statuses = ", ".join(sorted(ATTENDANCE_DEBIT_STATUSES))

    return {
        "items": items,
        "financial_allowed": financial_allowed,
        "source": roster_source or "manual",
        "status_labels": status_labels,
        "debit_policy": f"Списывается 1 занятие только для статусов {debit_statuses}",
        "can_edit": bool(window["is_open"]),
        "attendance_phase": window["phase"],
        "attendance_phase_message": window["message"],
        "attendance_starts_at": window["starts_at"],
        "attendance_mark_until": window["ends_at"],
        "planned_summary": {
            "will_come": sum(1 for i in items if i.get("planned_status") == "will_come"),
            "will_miss": sum(1 for i in items if i.get("planned_status") == "will_miss"),
        },
    }


@bp.route("/api/attendance/<int:schedule_id>", methods=["POST"])
def set_attendance(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    window = _attendance_marking_window_info(schedule)
    if not window["is_open"]:
        return {
            "error": "Окно отметки закрыто.",
            "attendance_phase": window["phase"],
            "attendance_phase_message": window["message"],
            "attendance_starts_at": window["starts_at"],
            "attendance_mark_until": window["ends_at"],
        }, 403

    # Money/finance info in response (lesson price, teacher payout) is only visible to system_settings users.
    financial_allowed = require_permission("system_settings") is None
    payout_percent = None
    booking_price = 0

    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if financial_allowed:
        try:
            payout_percent = int(get_setting_value(db, "teachers.payout_percent"))
        except Exception:
            payout_percent = 40
        if payout_percent < 0:
            payout_percent = 0
        if payout_percent > 100:
            payout_percent = 100

        if schedule.object_type == "individual" and schedule.object_id:
            lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first()
            if lesson and getattr(lesson, "booking_id", None):
                booking = db.query(BookingRequest).filter_by(id=lesson.booking_id).first()
                raw_amount = getattr(booking, "requested_amount", None) if booking else None
                booking_price = _safe_int(raw_amount)
                if booking_price is None and booking:
                    amount_before = _safe_int(getattr(booking, "amount_before_discount", None)) or 0
                    discount_amount = _safe_int(getattr(booking, "applied_discount_amount", None)) or 0
                    computed = amount_before - discount_amount
                    booking_price = computed if computed > 0 else 0
                if booking_price is None or booking_price < 0:
                    booking_price = 0

    data = request.json or {}
    items = data.get("items") or []
    if not isinstance(items, list):
        return {"error": "items должен быть списком"}, 400

    staff = _get_current_staff(db)
    results = []
    now = utcnow()

    for item in items:
        user_id = item.get("user_id")
        status = (item.get("status") or "").lower()
        comment = item.get("comment")
        if status not in ATTENDANCE_ALLOWED_STATUSES:
            return {"error": f"Недопустимый статус: {status}"}, 400
        if not user_id:
            return {"error": "user_id обязателен"}, 400
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return {"error": "user_id должен быть числом"}, 400

        att = db.query(Attendance).filter_by(schedule_id=schedule_id, user_id=user_id_int).first()
        if not att:
            att = Attendance(schedule_id=schedule_id, user_id=user_id_int)
            db.add(att)
        att.status = status
        att.comment = comment
        att.marked_at = now
        att.marked_by_staff_id = staff.id if staff else None

        if schedule.object_type == "group":
            if not att.abonement_id:
                group_id = schedule.group_id or schedule.object_id
                abon = _resolve_group_active_abonement(db, user_id_int, group_id, schedule.date) if group_id else None
                if abon:
                    att.abonement_id = abon.id

        db.flush()
        debited = _debit_abonement_for_attendance(db, att, staff)
        payload = {
            "user_id": user_id_int,
            "status": att.status,
            "comment": att.comment,
            "abonement_id": att.abonement_id,
            "debited": debited or _attendance_already_debited(db, att.id),
        }

        if financial_allowed:
            status_norm = str(att.status or "").strip().lower()
            counted = status_norm in ATTENDANCE_DEBIT_STATUSES
            if counted:
                lesson_price = _safe_int(getattr(att, "lesson_price_rub", None))
                if lesson_price is None and schedule.object_type == "group":
                    lesson_price = _safe_int(getattr(getattr(att, "abonement", None), "price_per_lesson_rub", None))
                    if lesson_price is None:
                        group_id = schedule.group_id or schedule.object_id
                        abon = _resolve_group_active_abonement(db, user_id_int, group_id, schedule.date) if group_id else None
                        lesson_price = _safe_int(getattr(abon, "price_per_lesson_rub", None)) if abon else None
                if lesson_price is None and schedule.object_type == "individual":
                    lesson_price = booking_price or 0

                lesson_price = 0 if lesson_price is None else lesson_price
                if lesson_price < 0:
                    lesson_price = 0

                percent = _safe_int(getattr(att, "teacher_percent", None))
                if percent is None:
                    percent = payout_percent
                if percent is None or percent < 0:
                    percent = 0
                if percent > 100:
                    percent = 100

                payout = _safe_int(getattr(att, "teacher_payout_rub", None))
                if payout is None:
                    payout = (lesson_price * percent) // 100 if lesson_price and percent else 0
            else:
                lesson_price = 0
                payout = 0
                percent = None

            payload.update({
                "counted": counted,
                "price_rub": lesson_price,
                "payout_rub": payout,
                "percent": percent,
            })

        results.append(payload)

    db.commit()
    return {"items": results}


@bp.route("/api/attendance/<int:schedule_id>/add-user", methods=["POST"])
def add_attendance_user(schedule_id):
    db = g.db
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    window = _attendance_marking_window_info(schedule)
    if not window["is_open"]:
        return {
            "error": "Окно отметки закрыто.",
            "attendance_phase": window["phase"],
            "attendance_phase_message": window["message"],
            "attendance_starts_at": window["starts_at"],
            "attendance_mark_until": window["ends_at"],
        }, 403

    data = request.json or {}
    user_id = data.get("user_id")
    if not user_id:
        return {"error": "user_id обязателен"}, 400
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return {"error": "user_id должен быть числом"}, 400

    user = db.query(User).filter_by(id=user_id_int).first()
    if not user:
        return {"error": "Пользователь не найден"}, 404

    existing = db.query(Attendance).filter_by(schedule_id=schedule_id, user_id=user_id_int).first()
    if existing:
        return {"message": "Пользователь уже в списке"}, 200

    att = Attendance(
        schedule_id=schedule_id,
        user_id=user_id_int,
        status=data.get("status") or "absent",
        comment=data.get("comment"),
    )
    db.add(att)
    db.commit()
    return {"message": "Добавлено", "user_id": user_id_int}


@bp.route("/api/teacher-payout/day", methods=["GET"])
def get_teacher_day_payout():
    db = g.db
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        return {"error": "date is required (YYYY-MM-DD)"}, 400
    try:
        date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"error": "date must be in YYYY-MM-DD format"}, 400

    teacher_id_raw = (request.args.get("teacher_id") or "").strip()
    teacher_id = None
    if teacher_id_raw:
        try:
            teacher_id = int(teacher_id_raw)
        except (TypeError, ValueError):
            return {"error": "teacher_id must be an integer"}, 400

    if not teacher_id:
        staff = _get_current_staff(db)
        if not staff:
            return {"error": "teacher_id is required"}, 400
        teacher_id = staff.id

    if teacher_id <= 0:
        return {"error": "teacher_id must be positive"}, 400

    # Financial info: only staff with access to system settings may view it.
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    try:
        payout_percent = int(get_setting_value(db, "teachers.payout_percent"))
    except Exception:
        payout_percent = 40
    if payout_percent < 0:
        payout_percent = 0
    if payout_percent > 100:
        payout_percent = 100

    # Some legacy schedule entries may miss Schedule.teacher_id for group/individual lessons.
    # We still need to include them by resolving the teacher via the linked Group/IndividualLesson.
    candidate_schedules = (
        db.query(Schedule)
        .filter(
            Schedule.date == date_val,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .all()
    )

    group_ids = {
        (s.group_id or s.object_id)
        for s in candidate_schedules
        if str(s.object_type or "").lower() == "group" and (s.group_id or s.object_id)
    }
    groups_by_id = {}
    if group_ids:
        groups_by_id = {g.id: g for g in db.query(Group).filter(Group.id.in_(group_ids)).all()}

    individual_lesson_ids = {
        s.object_id
        for s in candidate_schedules
        if str(s.object_type or "").lower() == "individual" and s.object_id
    }
    individual_lessons_by_id = {}
    booking_ids = set()
    if individual_lesson_ids:
        lessons = db.query(IndividualLesson).filter(IndividualLesson.id.in_(list(individual_lesson_ids))).all()
        individual_lessons_by_id = {l.id: l for l in lessons}
        booking_ids = {l.booking_id for l in lessons if getattr(l, "booking_id", None)}

    bookings_by_id = {}
    if booking_ids:
        bookings_by_id = {
            b.id: b
            for b in db.query(BookingRequest).filter(BookingRequest.id.in_(list(booking_ids))).all()
        }

    def _schedule_matches_teacher(schedule: Schedule) -> bool:
        if schedule.teacher_id:
            return int(schedule.teacher_id) == teacher_id

        object_type = str(schedule.object_type or "").lower()
        if object_type == "group":
            group_id = schedule.group_id or schedule.object_id
            if not group_id:
                return False
            group = groups_by_id.get(group_id)
            return bool(group and group.teacher_id == teacher_id)
        if object_type == "individual":
            if not schedule.object_id:
                return False
            lesson = individual_lessons_by_id.get(schedule.object_id)
            return bool(lesson and lesson.teacher_id == teacher_id)
        return False

    schedules = [s for s in candidate_schedules if _schedule_matches_teacher(s)]
    schedule_by_id = {s.id: s for s in schedules}
    schedule_ids = list(schedule_by_id.keys())

    attendance_rows = []
    if schedule_ids:
        attendance_rows = db.query(Attendance).filter(Attendance.schedule_id.in_(schedule_ids)).all()

    user_ids = {row.user_id for row in attendance_rows if row.user_id}
    users_by_id = {}
    if user_ids:
        users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(list(user_ids))).all()}

    attendance_by_schedule = {}
    for row in attendance_rows:
        attendance_by_schedule.setdefault(row.schedule_id, []).append(row)

    lessons_payload = []
    total_revenue = 0
    total_payout = 0

    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    for schedule in schedules:
        schedule_id = schedule.id
        object_type = str(schedule.object_type or "").lower()
        time_from = schedule.time_from or schedule.start_time
        time_to = schedule.time_to or schedule.end_time
        group_id = (schedule.group_id or schedule.object_id) if object_type == "group" else None
        group = groups_by_id.get(group_id) if group_id else None
        title = schedule.title or (group.name if group else None) or "\u0417\u0430\u043d\u044f\u0442\u0438\u0435"

        students_payload = []
        lesson_revenue = 0
        lesson_payout = 0

        for row in attendance_by_schedule.get(schedule_id, []):
            status = (row.status or "absent").strip().lower()
            counted = status in ATTENDANCE_DEBIT_STATUSES
            lesson_price = _safe_int(getattr(row, "lesson_price_rub", None))
            percent = _safe_int(getattr(row, "teacher_percent", None))
            payout = _safe_int(getattr(row, "teacher_payout_rub", None))

            if counted:
                if lesson_price is None:
                    if object_type == "group":
                        abon = getattr(row, "abonement", None)
                        if not abon and group_id:
                            abon = _resolve_group_active_abonement(db, row.user_id, group_id, schedule.date)
                        lesson_price = _safe_int(getattr(abon, "price_per_lesson_rub", None)) or 0
                    elif object_type == "individual":
                        lesson = individual_lessons_by_id.get(schedule.object_id) if schedule.object_id else None
                        booking = (
                            bookings_by_id.get(lesson.booking_id)
                            if (lesson and getattr(lesson, "booking_id", None))
                            else None
                        )
                        raw_amount = getattr(booking, "requested_amount", None) if booking else None
                        lesson_price = _safe_int(raw_amount)
                        if lesson_price is None and booking:
                            amount_before = _safe_int(getattr(booking, "amount_before_discount", None)) or 0
                            discount_amount = _safe_int(getattr(booking, "applied_discount_amount", None)) or 0
                            computed = amount_before - discount_amount
                            lesson_price = computed if computed > 0 else 0
                        if lesson_price is None or lesson_price < 0:
                            lesson_price = 0
                    else:
                        lesson_price = 0
                if percent is None:
                    percent = payout_percent
                if payout is None:
                    payout = (lesson_price * percent) // 100 if lesson_price and percent else 0
            else:
                lesson_price = 0
                payout = 0
                percent = None

            lesson_revenue += lesson_price
            lesson_payout += payout

            user = users_by_id.get(row.user_id)
            students_payload.append({
                "user_id": row.user_id,
                "name": user.name if user else None,
                "status": status,
                "counted": counted,
                "price_rub": lesson_price,
                "payout_rub": payout,
                "percent": percent,
            })

        total_revenue += lesson_revenue
        total_payout += lesson_payout

        students_payload.sort(key=lambda item: (item.get("name") or "", item.get("user_id") or 0))
        lessons_payload.append({
            "schedule_id": schedule_id,
            "object_type": object_type,
            "title": title,
            "group_id": group_id,
            "group_name": group.name if group else None,
            "direction_title": getattr(group.direction, "title", None) if group and group.direction else None,
            "time_from": time_from.strftime("%H:%M") if time_from else None,
            "time_to": time_to.strftime("%H:%M") if time_to else None,
            "students": students_payload,
            "lesson_revenue_rub": lesson_revenue,
            "lesson_payout_rub": lesson_payout,
        })

    lessons_payload.sort(key=lambda item: (item.get("time_from") or ""))

    return {
        "teacher_id": teacher_id,
        "date": date_val.isoformat(),
        "total_revenue_rub": total_revenue,
        "total_payout_rub": total_payout,
        "lessons": lessons_payload,
    }


@bp.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["GET"])
def get_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "Нельзя отметиться для этого занятия"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()
    return _serialize_attendance_intention_with_lock(row, lock_info), 200


@bp.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["POST"])
def set_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "Нельзя отметиться для этого занятия"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    if lock_info["is_locked"]:
        return {"error": ATTENDANCE_INTENTION_LOCKED_MESSAGE, "lock": lock_info}, 403

    payload = request.json or {}
    will_miss = payload.get("will_miss")
    if will_miss is None:
        will_miss = True
    else:
        will_miss = bool(will_miss)

    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()

    if not will_miss:
        if row:
            db.delete(row)
            db.commit()
        return _serialize_attendance_intention_with_lock(None, lock_info), 200

    reason = payload.get("reason")
    if isinstance(reason, str):
        reason = reason.strip() or None
    else:
        reason = None

    if not row:
        row = AttendanceIntention(
            schedule_id=schedule_id,
            user_id=user.id,
            status=ATTENDANCE_INTENTION_STATUS_WILL_MISS,
            source="user_web",
        )
        db.add(row)

    row.status = ATTENDANCE_INTENTION_STATUS_WILL_MISS
    row.reason = reason
    row.source = "user_web"

    db.commit()
    db.refresh(row)
    return _serialize_attendance_intention_with_lock(row, lock_info), 200


@bp.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["DELETE"])
def delete_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "Нельзя отметиться для этого занятия"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    if lock_info["is_locked"]:
        return {"error": ATTENDANCE_INTENTION_LOCKED_MESSAGE, "lock": lock_info}, 403

    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()
    if row:
        db.delete(row)
        db.commit()
    return _serialize_attendance_intention_with_lock(None, lock_info), 200

