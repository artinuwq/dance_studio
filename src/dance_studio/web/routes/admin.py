import json
import os
import re
import uuid
from datetime import date, datetime, time as dt_time, timedelta

import requests
from flask import Blueprint, current_app, g, jsonify, make_response, request, send_from_directory
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from dance_studio.core.abonement_notifications import (
    build_abonement_dispatch_ref,
    build_group_access_message,
    collect_group_access_items,
    resolve_group_ids_for_abonement,
)
from dance_studio.core.booking_amounts import compute_non_group_booking_base_amount
from dance_studio.core.notification_dispatch import (
    notification_dispatch_exists,
    record_notification_dispatch,
)
from dance_studio.core.statuses import (
    ABONEMENT_STATUS_ACTIVE,
    ABONEMENT_STATUS_CANCELLED,
    ABONEMENT_STATUS_EXPIRED,
    ABONEMENT_STATUS_PENDING_PAYMENT,
    BOOKING_NEGATIVE_STATUSES,
    BOOKING_PAYMENT_CONFIRMED_STATUSES,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_CREATED,
    BOOKING_STATUS_ATTENDED,
    BOOKING_STATUS_NO_SHOW,
    BOOKING_STATUS_WAITING_PAYMENT,
    set_abonement_status,
)
from dance_studio.core.notification_service import send_user_notification_sync
from dance_studio.core.personal_discounts import resolve_discount_usage_state
from dance_studio.core.config import BOT_TOKEN, OWNER_IDS, PROJECT_NAME_FULL, PROJECT_NAME_SHORT, TECH_ADMIN_ID
from dance_studio.core.media_manager import delete_user_photo
from dance_studio.core.system_settings_service import (
    SettingValidationError,
    get_setting_value,
    list_setting_changes,
    list_setting_specs,
    list_settings,
    update_setting,
)
from dance_studio.db.models import (
    Attendance,
    AuthIdentity,
    BookingRequest,
    Direction,
    DirectionUploadSession,
    Group,
    GroupAbonement,
    GroupAbonementActionLog,
    HallRental,
    IndividualLesson,
    Mailing,
    PaymentTransaction,
    Schedule,
    ScheduleOverrides,
    Staff,
    TeacherWorkingHours,
    User,
    UserDiscount,
)
from dance_studio.web.constants import (
    ALLOWED_DIRECTION_TYPES,
    BASE_DIR,
    FRONTEND_DIR,
    INACTIVE_SCHEDULE_STATUSES,
    MEDIA_ROOT,
    PROJECT_ROOT,
)
from dance_studio.web.services.api_errors import (
    internal_server_error_response,
    safe_client_error_message,
    token_fingerprint,
)
from dance_studio.web.services.access import _get_current_staff, get_current_user_from_request, require_permission
from dance_studio.web.services.admin import (
    _append_merge_note,
    _collect_busy_intervals,
    _has_slot_conflict,
    _merge_attendance_intentions_rows,
    _merge_attendance_reminders_rows,
    _merge_attendance_rows,
    _minutes_to_time_str,
    _parse_iso_date,
    _parse_month_start,
    _parse_user_id_for_merge,
    _schedule_group_id,
    _serialize_client_abonement_for_admin,
    _subtract_busy_intervals,
    _time_to_minutes,
    format_schedule,
    format_schedule_v2,
)
from dance_studio.web.services.bookings import get_group_occupancy_map
from dance_studio.web.services.media import _build_image_url, normalize_teaches, try_fetch_telegram_avatar
from dance_studio.web.services.studio_rules import (
    SERVICE_BREAK_END,
    SERVICE_BREAK_START,
    interval_overlaps_service_break,
)
from dance_studio.web.services.text import sanitize_plain_text

bp = Blueprint('admin_routes', __name__)

SCHEDULE_MOVE_TYPE_LABELS = {
    "studio_fault": "Перенос по вине студии",
    "absence_people": "Отсутствие людей",
    "low_attendance": "Нехватка людей",
}
INDIVIDUAL_SCHEDULE_MOVE_TYPE_LABELS = {
    "reschedule": "Перенос занятия",
}
RENTAL_SCHEDULE_MOVE_TYPE_LABELS = {
    "reschedule": "Перенос аренды",
}


def _sanitize_direction_title(value):
    return sanitize_plain_text(value, multiline=False) or ""


def _sanitize_direction_description(value):
    return sanitize_plain_text(value) or ""


def _serialize_direction_payload(direction, *, groups_count=None, include_status=False, include_updated_at=False):
    payload = {
        "direction_id": direction.direction_id,
        "direction_type": direction.direction_type or "dance",
        "title": _sanitize_direction_title(direction.title),
        "description": _sanitize_direction_description(direction.description),
        "base_price": direction.base_price,
        "is_popular": direction.is_popular,
        "image_path": _build_image_url(direction.image_path),
        "created_at": direction.created_at.isoformat(),
    }
    if groups_count is not None:
        payload["groups_count"] = groups_count
    if include_status:
        payload["status"] = direction.status
    if include_updated_at:
        payload["updated_at"] = direction.updated_at.isoformat()
    return payload
SCHEDULE_PRESENT_STATUSES = {"present", "late"}
NEGATIVE_BOOKING_STATUSES = set(BOOKING_NEGATIVE_STATUSES)
ACTIVE_NON_GROUP_BOOKING_STATUSES = {
    BOOKING_STATUS_CREATED,
    BOOKING_STATUS_WAITING_PAYMENT,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_ATTENDED,
    BOOKING_STATUS_NO_SHOW,
}
GROUP_ACCESS_NOTIFICATION_KEY = "group_access_links"

IMMUTABLE_STAFF_ROLE = "тех. админ"
STAFF_ROLE_RANKS = {
    "учитель": 10,
    "модератор": 20,
    "администратор": 30,
    "старший админ": 40,
    "владелец": 50,
    "тех. админ": 60,
}


def _normalize_staff_role(position: str | None) -> str:
    return str(position or "").strip().lower()


def _staff_role_rank(position: str | None) -> int:
    return STAFF_ROLE_RANKS.get(_normalize_staff_role(position), -1)


def _resolve_actor_staff_role(db) -> str:
    actor_staff = _get_current_staff(db)
    if actor_staff and actor_staff.position:
        return _normalize_staff_role(actor_staff.position)

    actor_telegram_id = getattr(g, "telegram_id", None)
    try:
        actor_telegram_id = int(actor_telegram_id)
    except (TypeError, ValueError):
        return ""

    if TECH_ADMIN_ID and actor_telegram_id == TECH_ADMIN_ID:
        return IMMUTABLE_STAFF_ROLE
    if actor_telegram_id in OWNER_IDS:
        return "владелец"
    return ""


def _can_edit_staff_by_roles(actor_role: str | None, target_role: str | None) -> bool:
    normalized_actor = _normalize_staff_role(actor_role)
    normalized_target = _normalize_staff_role(target_role)

    if normalized_target == IMMUTABLE_STAFF_ROLE:
        return False
    if normalized_actor == IMMUTABLE_STAFF_ROLE:
        return True

    actor_rank = _staff_role_rank(normalized_actor)
    target_rank = _staff_role_rank(normalized_target)
    if actor_rank < 0 or target_rank < 0:
        return False
    return actor_rank >= target_rank


def _parse_stats_date_range(date_from_raw: str | None, date_to_raw: str | None) -> tuple[date | None, date | None]:
    try:
        date_from_val = datetime.strptime(date_from_raw, "%Y-%m-%d").date() if date_from_raw else None
        date_to_val = datetime.strptime(date_to_raw, "%Y-%m-%d").date() if date_to_raw else None
    except ValueError as exc:
        raise ValueError("Неверный формат даты, используйте YYYY-MM-DD") from exc

    if date_from_val and date_to_val and date_from_val > date_to_val:
        raise ValueError("date_from не должен быть позже date_to")

    return date_from_val, date_to_val


def _stats_datetime_bounds(date_from_val: date | None, date_to_val: date | None) -> tuple[datetime | None, datetime | None]:
    start_dt = datetime.combine(date_from_val, dt_time.min) if date_from_val else None
    end_dt = datetime.combine(date_to_val, dt_time.max) if date_to_val else None
    return start_dt, end_dt


def _safe_non_negative_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _booking_duration_minutes(booking: BookingRequest) -> int | None:
    direct_duration = _safe_non_negative_int(getattr(booking, "duration_minutes", None))
    if direct_duration:
        return direct_duration
    if not booking.time_from or not booking.time_to:
        return None
    start_dt = datetime.combine(date.today(), booking.time_from)
    end_dt = datetime.combine(date.today(), booking.time_to)
    delta_minutes = int((end_dt - start_dt).total_seconds() // 60)
    return delta_minutes if delta_minutes > 0 else None


def _safe_int_setting_value(db, key: str) -> int | None:
    try:
        return int(get_setting_value(db, key))
    except (TypeError, ValueError):
        return None


def _booking_expected_amount_rub(db, booking: BookingRequest) -> int | None:
    requested_amount = _safe_non_negative_int(getattr(booking, "requested_amount", None))
    if requested_amount is not None:
        return requested_amount

    object_type = str(getattr(booking, "object_type", "") or "").strip().lower()
    if object_type in {"individual", "rental"}:
        duration_minutes = _booking_duration_minutes(booking)
        if duration_minutes is None:
            return None
        return compute_non_group_booking_base_amount(
            db,
            object_type=object_type,
            duration_minutes=duration_minutes,
        )

    if object_type != "group":
        return None

    lessons_count = _safe_non_negative_int(getattr(booking, "lessons_count", None))
    if not lessons_count:
        return 0 if lessons_count == 0 else None

    group_id = getattr(booking, "group_id", None)
    if not group_id:
        return None

    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return None

    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first()
    if not direction:
        return None

    base_price = _safe_non_negative_int(direction.base_price)
    if base_price is None:
        return None

    return lessons_count * base_price


def _can_assign_staff_role_by_roles(actor_role: str | None, new_role: str | None) -> bool:
    normalized_actor = _normalize_staff_role(actor_role)
    normalized_new = _normalize_staff_role(new_role)

    if normalized_new == IMMUTABLE_STAFF_ROLE:
        return False
    if normalized_actor == IMMUTABLE_STAFF_ROLE:
        return True

    actor_rank = _staff_role_rank(normalized_actor)
    target_rank = _staff_role_rank(normalized_new)
    if actor_rank < 0 or target_rank < 0:
        return False
    return actor_rank >= target_rank


def _staff_edit_guard(db, target_staff: Staff):
    actor_role = _resolve_actor_staff_role(db)
    target_role = _normalize_staff_role(target_staff.position)
    actor_staff = _get_current_staff(db)
    is_self_target = bool(actor_staff and target_staff and actor_staff.id == target_staff.id)
    if target_role == IMMUTABLE_STAFF_ROLE and is_self_target:
        return None
    if _can_edit_staff_by_roles(actor_role, target_role):
        return None
    if target_role == IMMUTABLE_STAFF_ROLE:
        return {"error": "Профиль тех. админа нельзя изменять"}, 403
    return {"error": "Нельзя редактировать сотрудника с ролью выше вашей"}, 403


def _staff_assignment_guard(db, new_role: str | None):
    actor_role = _resolve_actor_staff_role(db)
    normalized_new_role = _normalize_staff_role(new_role)
    if _can_assign_staff_role_by_roles(actor_role, normalized_new_role):
        return None
    if normalized_new_role == IMMUTABLE_STAFF_ROLE:
        return {"error": "Роль тех. админ нельзя назначать через персонал"}, 403
    return {"error": "Нельзя назначить роль выше вашей"}, 403


def _staff_editability_payload(db, staff: Staff) -> tuple[bool, str | None]:
    target_role = _normalize_staff_role(staff.position)
    actor_staff = _get_current_staff(db)
    is_self_target = bool(actor_staff and staff and actor_staff.id == staff.id)
    if target_role == IMMUTABLE_STAFF_ROLE and is_self_target:
        return True, None
    can_edit = _can_edit_staff_by_roles(_resolve_actor_staff_role(db), target_role)
    if can_edit:
        return True, None
    if target_role == IMMUTABLE_STAFF_ROLE:
        return False, "Профиль тех. админа нельзя изменять"
    return False, "Нельзя редактировать сотрудника с ролью выше вашей"


@bp.route("/")
def index():
    response = make_response(send_from_directory(FRONTEND_DIR, "index.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.route("/health")
def health():
    return {"status": "ok"}


@bp.route("/bot-username")
def get_bot_username():
    """Возвращает username бота для открытия чата."""
    db = g.db
    try:
        configured = get_setting_value(db, "contacts.bot_username")
        db.commit()
        if isinstance(configured, str) and configured.strip():
            return jsonify({"bot_username": configured.strip().lstrip("@")})
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed to resolve bot username from system settings")

    try:
        from dance_studio.bot.bot import BOT_USERNAME_GLOBAL
        if BOT_USERNAME_GLOBAL:
            return jsonify({"bot_username": str(BOT_USERNAME_GLOBAL).strip().lstrip("@")})
    except Exception:
        current_app.logger.exception("Failed to resolve runtime bot username")

    return jsonify({"bot_username": "dance_studio_admin_bot"})


def _schedule_time_bounds(schedule: Schedule):
    time_from = schedule.time_from or schedule.start_time
    time_to = schedule.time_to or schedule.end_time
    return time_from, time_to


def _format_schedule_slot_label(schedule: Schedule) -> str:
    if not schedule.date:
        return "—"
    time_from, time_to = _schedule_time_bounds(schedule)
    date_text = schedule.date.strftime("%d.%m.%Y")
    if not time_from or not time_to:
        return date_text
    return f"{date_text} {time_from.strftime('%H:%M')}–{time_to.strftime('%H:%M')}"


def _active_group_abonements_for_schedule_date(db, group_id: int, schedule_date: date | None) -> dict[int, GroupAbonement]:
    query = db.query(GroupAbonement).filter(
        GroupAbonement.group_id == group_id,
        GroupAbonement.status == ABONEMENT_STATUS_ACTIVE,
    )
    if schedule_date:
        day_start = datetime.combine(schedule_date, dt_time.min)
        day_end = datetime.combine(schedule_date, dt_time.max)
        query = query.filter(
            or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= day_end),
            or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= day_start),
        )
    rows = query.order_by(GroupAbonement.valid_to.is_(None), GroupAbonement.valid_to.asc(), GroupAbonement.id.asc()).all()
    by_user: dict[int, GroupAbonement] = {}
    for row in rows:
        by_user.setdefault(row.user_id, row)
    return by_user


def _parse_apply_bundle_flag(payload: dict | None, *, default: bool = True) -> bool:
    if not isinstance(payload, dict):
        return default
    raw_value = payload.get("apply_bundle", None)
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError("apply_bundle должен быть boolean")


def _collect_target_abonements(
    db,
    abonement: GroupAbonement,
    *,
    apply_bundle: bool,
) -> list[GroupAbonement]:
    bundle_id = str(getattr(abonement, "bundle_id", "") or "").strip()
    if not apply_bundle or not bundle_id:
        return [abonement]

    rows = (
        db.query(GroupAbonement)
        .filter(
            GroupAbonement.user_id == abonement.user_id,
            GroupAbonement.bundle_id == bundle_id,
        )
        .order_by(GroupAbonement.id.asc())
        .all()
    )
    return rows if len(rows) > 1 else [abonement]


def _extend_abonement_by_week(
    db,
    *,
    abonement: GroupAbonement,
    action_type: str,
    reason: str,
    staff: Staff | None,
    note: str,
    payload: dict,
) -> bool:
    duplicate = db.query(GroupAbonementActionLog.id).filter_by(
        abonement_id=abonement.id,
        action_type=action_type,
        reason=reason,
    ).first()
    if duplicate:
        return False

    now = datetime.utcnow()
    if abonement.valid_to:
        base = abonement.valid_to if abonement.valid_to > now else now
    elif abonement.valid_from:
        base = abonement.valid_from if abonement.valid_from > now else now
    else:
        base = now
    abonement.valid_to = base + timedelta(days=7)

    db.add(
        GroupAbonementActionLog(
            abonement_id=abonement.id,
            action_type=action_type,
            credits_delta=0,
            reason=reason,
            note=note,
            actor_type="staff",
            actor_id=staff.id if staff else None,
            payload=json.dumps(payload, ensure_ascii=False),
        )
    )
    return True


def _refund_schedule_attendance_credit(
    db,
    *,
    attendance: Attendance,
    action_type: str,
    reason: str,
    staff: Staff | None,
    note: str,
    payload: dict,
) -> bool:
    if not attendance.abonement_id:
        return False

    debit_exists = db.query(GroupAbonementActionLog.id).filter_by(
        attendance_id=attendance.id,
        action_type="debit_attendance",
    ).first()
    if not debit_exists:
        return False

    duplicate_refund = db.query(GroupAbonementActionLog.id).filter_by(
        attendance_id=attendance.id,
        action_type=action_type,
        reason=reason,
    ).first()
    if duplicate_refund:
        return False

    abonement = db.query(GroupAbonement).filter_by(id=attendance.abonement_id).first()
    if not abonement:
        return False
    abonement.balance_credits = int(abonement.balance_credits or 0) + 1

    db.add(
        GroupAbonementActionLog(
            abonement_id=abonement.id,
            action_type=action_type,
            credits_delta=1,
            reason=reason,
            note=note,
            attendance_id=attendance.id,
            actor_type="staff",
            actor_id=staff.id if staff else None,
            payload=json.dumps(payload, ensure_ascii=False),
        )
    )
    return True


def _send_group_chat_message(chat_id: int | None, text: str) -> tuple[bool, str | None]:
    if not chat_id:
        return False, "group_chat_not_configured"
    if not BOT_TOKEN:
        return False, "bot_token_not_set"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": int(chat_id),
                "text": text,
            },
            timeout=10,
        )
        if resp.ok:
            return True, None
        return False, "telegram_send_failed"
    except Exception:
        current_app.logger.exception("Failed to notify group chat")
        return False, "telegram_send_failed"


def _has_group_schedule_conflict(
    db,
    *,
    schedule_id: int,
    group_id: int,
    target_date: date,
    target_time_from,
    target_time_to,
) -> bool:
    rows = (
        db.query(Schedule)
        .filter(
            Schedule.id != schedule_id,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
            Schedule.date == target_date,
            or_(Schedule.group_id == group_id, Schedule.object_id == group_id),
        )
        .all()
    )
    start_target = _time_to_minutes(target_time_from)
    end_target = _time_to_minutes(target_time_to)
    for row in rows:
        row_from, row_to = _schedule_time_bounds(row)
        if not row_from or not row_to:
            continue
        start_row = _time_to_minutes(row_from)
        end_row = _time_to_minutes(row_to)
        if start_target < end_row and start_row < end_target:
            return True
    return False


def _load_individual_lesson_for_schedule(db, schedule: Schedule) -> IndividualLesson | None:
    if str(schedule.object_type or "").strip().lower() != "individual":
        return None
    if not schedule.object_id:
        return None
    return db.query(IndividualLesson).filter_by(id=schedule.object_id).first()


def _load_rental_for_schedule(db, schedule: Schedule) -> HallRental | None:
    if str(schedule.object_type or "").strip().lower() != "rental":
        return None
    if not schedule.object_id:
        return None
    return db.query(HallRental).filter_by(id=schedule.object_id).first()


def _duration_minutes_between(time_from: dt_time | None, time_to: dt_time | None) -> int | None:
    if not time_from or not time_to:
        return None
    start_min = _time_to_minutes(time_from)
    end_min = _time_to_minutes(time_to)
    if end_min <= start_min:
        return None
    return end_min - start_min


def _sync_individual_lesson_with_schedule(
    lesson: IndividualLesson,
    *,
    target_date: date | None = None,
    target_time_from: dt_time | None = None,
    target_time_to: dt_time | None = None,
    status: str | None = None,
    staff: Staff | None = None,
) -> None:
    if target_date is not None:
        lesson.date = target_date
    if target_time_from is not None:
        lesson.time_from = target_time_from
    if target_time_to is not None:
        lesson.time_to = target_time_to

    if target_time_from is not None or target_time_to is not None:
        lesson.duration_minutes = _duration_minutes_between(lesson.time_from, lesson.time_to)

    if status is not None:
        lesson.status = status
        lesson.status_updated_at = datetime.now()
        lesson.status_updated_by_id = staff.id if staff else None


def _sync_rental_with_schedule(
    rental: HallRental,
    *,
    target_date: date | None = None,
    target_time_from: dt_time | None = None,
    target_time_to: dt_time | None = None,
    status: str | None = None,
    cancelled: bool = False,
) -> None:
    if target_date is not None:
        rental.date = target_date
    if target_time_from is not None:
        rental.time_from = target_time_from
    if target_time_to is not None:
        rental.time_to = target_time_to

    if rental.date and rental.time_from:
        rental.start_time = datetime.combine(rental.date, rental.time_from)
    if rental.date and rental.time_to:
        rental.end_time = datetime.combine(rental.date, rental.time_to)
    if target_time_from is not None or target_time_to is not None:
        rental.duration_minutes = _duration_minutes_between(rental.time_from, rental.time_to)

    if status is not None:
        rental.status = status
    if cancelled:
        rental.activity_status = "cancelled"
    else:
        current_activity_status = str(getattr(rental, "activity_status", "") or "").strip().lower()
        if current_activity_status == "cancelled":
            rental.activity_status = "active"


def _has_teacher_schedule_conflict(
    db,
    *,
    schedule_id: int,
    teacher_id: int,
    target_date: date,
    target_time_from: dt_time,
    target_time_to: dt_time,
    lesson_id: int | None = None,
) -> bool:
    rows = (
        db.query(Schedule)
        .filter(
            Schedule.id != schedule_id,
            Schedule.teacher_id == teacher_id,
            Schedule.date == target_date,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .all()
    )
    start_target = _time_to_minutes(target_time_from)
    end_target = _time_to_minutes(target_time_to)
    for row in rows:
        row_from, row_to = _schedule_time_bounds(row)
        if not row_from or not row_to:
            continue
        start_row = _time_to_minutes(row_from)
        end_row = _time_to_minutes(row_to)
        if start_target < end_row and start_row < end_target:
            return True

    lessons = (
        db.query(IndividualLesson)
        .filter(
            IndividualLesson.teacher_id == teacher_id,
            IndividualLesson.date == target_date,
        )
        .all()
    )
    for lesson in lessons:
        if lesson_id and lesson.id == lesson_id:
            continue
        if str(lesson.status or "").strip().lower() in {"cancelled", "canceled"}:
            continue
        if not lesson.time_from or not lesson.time_to:
            continue
        start_row = _time_to_minutes(lesson.time_from)
        end_row = _time_to_minutes(lesson.time_to)
        if start_target < end_row and start_row < end_target:
            return True

    return False


def _has_hall_schedule_conflict(
    db,
    *,
    schedule_id: int,
    target_date: date,
    target_time_from: dt_time,
    target_time_to: dt_time,
) -> bool:
    rows = (
        db.query(Schedule)
        .filter(
            Schedule.id != schedule_id,
            Schedule.date == target_date,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .all()
    )
    start_target = _time_to_minutes(target_time_from)
    end_target = _time_to_minutes(target_time_to)
    for row in rows:
        row_from, row_to = _schedule_time_bounds(row)
        if not row_from or not row_to:
            continue
        start_row = _time_to_minutes(row_from)
        end_row = _time_to_minutes(row_to)
        if start_target < end_row and start_row < end_target:
            return True
    return False


def _notify_individual_student(
    db,
    lesson: IndividualLesson,
    text: str,
    *,
    context_note: str,
) -> tuple[bool, str | None]:
    if not lesson.student_id:
        return False, "student_not_found"

    student = db.query(User).filter_by(id=lesson.student_id).first()
    if not student:
        return False, "student_not_found"
    if not student.telegram_id:
        return False, "student_telegram_not_configured"

    try:
        notified = send_user_notification_sync(
            user_id=int(student.telegram_id),
            text=text,
            context_note=context_note,
        )
        if notified:
            return True, None
        return False, "telegram_send_failed"
    except Exception:
        current_app.logger.exception("Failed to notify individual student")
        return False, "telegram_send_failed"


def _notify_rental_creator(
    db,
    rental: HallRental,
    text: str,
    *,
    context_note: str,
) -> tuple[bool, str | None]:
    creator_type = str(rental.creator_type or "").strip().lower()
    telegram_id = None

    if creator_type == "user":
        creator = db.query(User).filter_by(id=rental.creator_id).first()
        telegram_id = creator.telegram_id if creator else None
    elif creator_type == "teacher":
        creator = db.query(Staff).filter_by(id=rental.creator_id).first()
        telegram_id = creator.telegram_id if creator else None
    else:
        return False, "creator_not_supported"

    if not telegram_id:
        return False, "creator_telegram_not_configured"

    try:
        notified = send_user_notification_sync(
            user_id=int(telegram_id),
            text=text,
            context_note=context_note,
        )
        if notified:
            return True, None
        return False, "telegram_send_failed"
    except Exception:
        current_app.logger.exception("Failed to notify rental creator")
        return False, "telegram_send_failed"


def _notify_abonement_group_access_links(
    db,
    abonement: GroupAbonement,
) -> tuple[bool, str | None]:
    user = db.query(User).filter_by(id=abonement.user_id).first()
    if not user:
        return False, "user_not_found"
    if not user.telegram_id:
        return False, "user_telegram_not_configured"

    entity_ref = build_abonement_dispatch_ref(abonement)
    if notification_dispatch_exists(
        db,
        notification_key=GROUP_ACCESS_NOTIFICATION_KEY,
        entity_type="abonement",
        entity_ref=entity_ref,
        recipient_ref=user.telegram_id,
        statuses={"sent"},
    ):
        return True, "already_sent"

    group_ids = resolve_group_ids_for_abonement(db, abonement)
    group_items = collect_group_access_items(db, group_ids)
    message_text = build_group_access_message(group_items)
    if not message_text:
        return False, "group_access_message_empty"

    try:
        notified = send_user_notification_sync(
            user_id=int(user.telegram_id),
            text=message_text,
            context_note=f"Ссылки на чаты групп по абонементу #{abonement.id}",
        )
        record_notification_dispatch(
            db,
            notification_key=GROUP_ACCESS_NOTIFICATION_KEY,
            entity_type="abonement",
            entity_ref=entity_ref,
            recipient_ref=user.telegram_id,
            status="sent" if notified else "failed",
            payload={"abonement_id": abonement.id},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return True, "already_sent"
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed to notify abonement owner about group access links")
        return False, "telegram_send_failed"

    if notified:
        return True, None
    return False, "telegram_send_failed"


def _resolve_rental_creator_summary(db, rental: HallRental | None) -> dict:
    payload = {
        "creator_id": rental.creator_id if rental else None,
        "creator_type": rental.creator_type if rental else None,
        "creator_name": None,
        "creator_username": None,
        "creator_telegram_id": None,
        "purpose_text": None,
        "contact_text": None,
    }
    if not rental:
        return payload

    raw_lines: list[str] = []
    for raw_text in (rental.purpose, rental.comment):
        if not raw_text:
            continue
        for line in str(raw_text).splitlines():
            cleaned = line.strip()
            if cleaned:
                raw_lines.append(cleaned)

    purpose_lines: list[str] = []
    seen_purpose_lines: set[str] = set()
    for line in raw_lines:
        normalized = line.casefold()
        if normalized.startswith("контакт:"):
            contact_value = line.split(":", 1)[1].strip() if ":" in line else ""
            if contact_value and not payload["contact_text"]:
                payload["contact_text"] = contact_value
            continue
        if normalized not in seen_purpose_lines:
            seen_purpose_lines.add(normalized)
            purpose_lines.append(line)
    if purpose_lines:
        payload["purpose_text"] = "\n".join(purpose_lines)

    creator_type = str(rental.creator_type or "").strip().lower()
    if creator_type == "user":
        creator = db.query(User).filter_by(id=rental.creator_id).first()
        if creator:
            payload["creator_name"] = creator.name
            payload["creator_username"] = creator.username
            payload["creator_telegram_id"] = creator.telegram_id
    elif creator_type == "teacher":
        creator = db.query(Staff).filter_by(id=rental.creator_id).first()
        if creator:
            payload["creator_name"] = creator.name
            payload["creator_telegram_id"] = creator.telegram_id

    return payload


@bp.route("/schedule")
def schedule():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error
    db = g.db
    data = db.query(Schedule).all()
    return jsonify([format_schedule(s) for s in data])


@bp.route("/schedule/public")
def schedule_public():
    db = g.db
    mine_flag = request.args.get("mine")
    user = get_current_user_from_request(db)
    mine = str(mine_flag).lower() in {"1", "true", "yes", "y"} if mine_flag is not None else bool(user)

    query = db.query(Schedule).outerjoin(IndividualLesson, Schedule.object_id == IndividualLesson.id)\
                               .outerjoin(HallRental, Schedule.object_id == HallRental.id)

    # базовый фильтр по статусу
    query = query.filter(Schedule.status != "cancelled")

    if mine and user:
        today = date.today()
        mine_conditions = []

        # Индивидуальные занятия пользователя
        mine_conditions.append(
            (Schedule.object_type == "individual") & (IndividualLesson.student_id == user.id)
        )
        # Аренда, созданная пользователем
        mine_conditions.append(
            (Schedule.object_type == "rental") & (HallRental.creator_type == "user") & (HallRental.creator_id == user.id)
        )

        # Группы пользователя по активным абонементам
        active_group_ids = [
            gid for (gid,) in db.query(GroupAbonement.group_id).filter(
                GroupAbonement.user_id == user.id,
                GroupAbonement.status == ABONEMENT_STATUS_ACTIVE,
                or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= today),
                or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= today),
            ).all()
        ]
        if active_group_ids:
            mine_conditions.append(
                (Schedule.object_type == "group") & (
                    (Schedule.group_id.in_(active_group_ids)) |
                    (Schedule.object_id.in_(active_group_ids))
                )
            )

        # Если пользователь связан с сотрудником (преподаватель) — добавляем его группы
        staff = None
        if getattr(user, "id", None):
            staff = db.query(Staff).filter_by(user_id=user.id).first()
        if not staff and getattr(user, "telegram_id", None):
            staff = db.query(Staff).filter_by(telegram_id=user.telegram_id).first()
        if staff:
            taught_group_ids = [gid for (gid,) in db.query(Group.id).filter(Group.teacher_id == staff.id).all()]
            staff_group_parts = [Schedule.teacher_id == staff.id]
            if taught_group_ids:
                staff_group_parts.append(Schedule.group_id.in_(taught_group_ids))
                staff_group_parts.append(Schedule.object_id.in_(taught_group_ids))
            mine_conditions.append((Schedule.object_type == "group") & or_(*staff_group_parts))

        if mine_conditions:
            query = query.filter(or_(*mine_conditions))
        else:
            query = query.filter(Schedule.id == -1)
    else:
        # публичная выдача только групп
        query = query.filter(Schedule.object_type == "group")

    items = query.all()

    result = []
    for s in items:
        time_from = s.time_from or s.start_time
        time_to = s.time_to or s.end_time

        entry = {
            "id": s.id,
            "object_type": s.object_type,
            "date": s.date.isoformat() if s.date else None,
            "start": str(time_from) if time_from else None,
            "end": str(time_to) if time_to else None,
        }

        if s.object_type == "group":
            group = None
            if s.group_id:
                group = db.query(Group).filter_by(id=s.group_id).first()
            elif s.object_id:
                group = db.query(Group).filter_by(id=s.object_id).first()

            direction_title = None
            direction_description = None
            direction_image = None
            direction_id = None
            teacher_name = None
            lessons_per_week = None
            age_group = None
            if group:
                if group.direction_id:
                    direction_id = group.direction_id
                    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first()
                    if direction:
                        direction_title = _sanitize_direction_title(direction.title)
                        direction_description = _sanitize_direction_description(direction.description)
                        direction_image = _build_image_url(direction.image_path)
                if group.teacher_id:
                    teacher = db.query(Staff).filter_by(id=group.teacher_id).first()
                    teacher_name = teacher.name if teacher else None
                lessons_per_week = group.lessons_per_week
                age_group = group.age_group

            entry.update({
                "title": group.name if group and group.name else s.title,
                "direction": direction_title,
                "direction_description": direction_description,
                "direction_image": direction_image,
                "direction_id": direction_id,
                "teacher_name": teacher_name,
                "lessons_per_week": lessons_per_week,
                "age_group": age_group,
            })
        elif s.object_type == "individual":
            lesson = db.query(IndividualLesson).filter_by(id=s.object_id).first() if s.object_id else None
            teacher = db.query(Staff).filter_by(id=s.teacher_id).first() if s.teacher_id else None
            entry.update({
                "title": s.title or "Индивидуальное занятие",
                "teacher_name": teacher.name if teacher else None,
                "student_id": lesson.student_id if lesson else None,
                "status": s.status,
            })
        elif s.object_type == "rental":
            rental = db.query(HallRental).filter_by(id=s.object_id).first() if s.object_id else None
            entry.update({
                "title": s.title or "Аренда зала",
                "status": s.status,
            })
            entry.update(_resolve_rental_creator_summary(db, rental))
        else:
            entry["title"] = s.title

        result.append(entry)

    return jsonify(result)


@bp.route("/schedule/v2", methods=["GET"])
def schedule_v2_list():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    query = db.query(Schedule)
    object_type = request.args.get("object_type")
    teacher_id = request.args.get("teacher_id")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    mine_flag = request.args.get("mine")
    mine = str(mine_flag).lower() in {"1", "true", "yes", "y"} if mine_flag is not None else False

    if object_type:
        query = query.filter(Schedule.object_type == object_type)
    if teacher_id:
        try:
            teacher_id_val = int(teacher_id)
        except (TypeError, ValueError):
            return {"error": "teacher_id must be an integer"}, 400
        if teacher_id_val <= 0:
            return {"error": "teacher_id must be positive"}, 400
        query = query.filter(Schedule.teacher_id == teacher_id_val)
    if date_from:
        try:
            date_from_val = datetime.strptime(date_from, "%Y-%m-%d").date()
            query = query.filter(Schedule.date >= date_from_val)
        except ValueError:
            return {"error": "date_from должен быть в формате YYYY-MM-DD"}, 400
    if date_to:
        try:
            date_to_val = datetime.strptime(date_to, "%Y-%m-%d").date()
            query = query.filter(Schedule.date <= date_to_val)
        except ValueError:
            return {"error": "date_to должен быть в формате YYYY-MM-DD"}, 400

    if mine:
        user = get_current_user_from_request(db)
        if not user:
            return {"error": "Требуется авторизация"}, 401
        query = query.outerjoin(IndividualLesson, Schedule.object_id == IndividualLesson.id)\
                     .outerjoin(HallRental, Schedule.object_id == HallRental.id)\
                     .filter(
                         or_(
                             (Schedule.object_type == "individual") & (IndividualLesson.student_id == user.id),
                             (Schedule.object_type == "rental") & (HallRental.creator_type == "user") & (HallRental.creator_id == user.id)
                         )
                     )

    data = query.all()
    result = []
    for s in data:
        payload = format_schedule_v2(s)
        if s.object_type == "rental":
            rental = db.query(HallRental).filter_by(id=s.object_id).first() if s.object_id else None
            payload["title"] = s.title or "Аренда зала"
            payload.update(_resolve_rental_creator_summary(db, rental))
        result.append(payload)
    return jsonify(result)


@bp.route("/schedule", methods=["POST"])
def create_schedule():
    """
    Создает новое занятие
    """
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}

    

    if not data.get("title") or not data.get("teacher_id") or not data.get("date") or not data.get("start_time") or not data.get("end_time"):
        return {"error": "title, teacher_id, date, start_time и end_time обязательны"}, 400
    
    teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
    if not teacher:
        return {"error": "Учитель не найден"}, 404
    
    date_val = datetime.strptime(data["date"], "%Y-%m-%d").date()
    start_time_val = datetime.strptime(data["start_time"], "%H:%M").time()
    end_time_val = datetime.strptime(data["end_time"], "%H:%M").time()
    if start_time_val >= end_time_val:
        return {"error": "start_time must be earlier than end_time"}, 400
    if interval_overlaps_service_break(start_time_val, end_time_val):
        return {"error": "Selected interval overlaps service break 14:30-15:00"}, 400

    schedule = Schedule(
        title=data["title"],
        teacher_id=data["teacher_id"],
        date=date_val,
        start_time=start_time_val,
        end_time=end_time_val
    )
    db.add(schedule)
    db.commit()
    
    return format_schedule(schedule), 201


@bp.route("/schedule/v2", methods=["POST"])
def create_schedule_v2():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}

    object_type = data.get("object_type")
    object_id = data.get("object_id")
    date_str = data.get("date")
    time_from_str = data.get("time_from")
    time_to_str = data.get("time_to")
    repeat_until_str = data.get("repeat_weekly_until")

    if object_type not in ["group", "individual", "rental"]:
        return {"error": "object_type должен быть одним из: group, individual, rental"}, 400
    if not object_id:
        return {"error": "object_id обязателен"}, 400
    if not date_str or not time_from_str or not time_to_str:
        return {"error": "date, time_from, time_to обязательны"}, 400

    try:
        date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
        time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
    except ValueError:
        return {"error": "Неверный формат даты или времени"}, 400

    if time_from_val >= time_to_val:
        return {"error": "time_from должен быть меньше time_to"}, 400
    if interval_overlaps_service_break(time_from_val, time_to_val):
        return {"error": "Selected interval overlaps service break 14:30-15:00"}, 400

    group_id = data.get("group_id")
    teacher_id = data.get("teacher_id")

    title = None
    if object_type == "group":
        group = db.query(Group).filter_by(id=object_id).first()
        if not group:
            return {"error": "Группа не найдена"}, 404
        group_id = group.id
        teacher_id = group.teacher_id
        title = group.name
    elif object_type == "individual":
        lesson = db.query(IndividualLesson).filter_by(id=object_id).first()
        if not lesson:
            return {"error": "Индивидуальное занятие не найдено"}, 404
        teacher_id = lesson.teacher_id
        title = "Индивидуальное занятие"
    elif object_type == "rental":
        rental = db.query(HallRental).filter_by(id=object_id).first()
        if not rental:
            return {"error": "Аренда не найдена"}, 404
        title = "Аренда зала"

    def build_entry(entry_date):
        return Schedule(
            object_id=object_id,
            object_type=object_type,
            date=entry_date,
            time_from=time_from_val,
            time_to=time_to_val,
            status=data.get("status", "scheduled"),
            status_comment=data.get("status_comment"),
            updated_by=data.get("updated_by"),
            group_id=group_id,
            teacher_id=teacher_id,
            title=title or f"{object_type} #{object_id}",
            start_time=time_from_val,
            end_time=time_to_val
        )

    entries = [build_entry(date_val)]
    if repeat_until_str:
        try:
            repeat_until = datetime.strptime(repeat_until_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "repeat_weekly_until должен быть в формате YYYY-MM-DD"}, 400
        current_date = date_val
        while True:
            current_date = current_date + timedelta(days=7)
            if current_date > repeat_until:
                break
            entries.append(build_entry(current_date))

    for entry in entries:
        db.add(entry)
    db.commit()

    return jsonify([format_schedule_v2(s) for s in entries]), 201


@bp.route("/schedule/<int:schedule_id>", methods=["PUT"])
def update_schedule(schedule_id):
    """
    Обновляет существующее занятие
    """
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    
    data = request.json
    
    if data.get("title"):
        schedule.title = data["title"]
    if data.get("teacher_id"):
        teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
        if not teacher:
            return {"error": "Учитель не найден"}, 404
        schedule.teacher_id = data["teacher_id"]
    if data.get("date"):
        schedule.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    if data.get("start_time"):
        schedule.start_time = datetime.strptime(data["start_time"], "%H:%M").time()
    if data.get("end_time"):
        schedule.end_time = datetime.strptime(data["end_time"], "%H:%M").time()

    time_fields_changed = bool(data.get("start_time") or data.get("end_time"))
    if time_fields_changed:
        start_candidate = schedule.start_time or schedule.time_from
        end_candidate = schedule.end_time or schedule.time_to
        if start_candidate and end_candidate:
            if start_candidate >= end_candidate:
                return {"error": "start_time must be earlier than end_time"}, 400
            if interval_overlaps_service_break(start_candidate, end_candidate):
                return {"error": "Selected interval overlaps service break 14:30-15:00"}, 400
    
    db.commit()
    
    return format_schedule(schedule)


@bp.route("/schedule/v2/<int:schedule_id>", methods=["PUT"])
def update_schedule_v2(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    data = request.json or {}

    if "date" in data:
        try:
            schedule.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date должен быть в формате YYYY-MM-DD и быть существующей датой"}, 400
    if "time_from" in data:
        try:
            schedule.time_from = datetime.strptime(data["time_from"], "%H:%M").time()
        except ValueError:
            return {"error": "time_from должен быть в формате HH:MM"}, 400
    if "time_to" in data:
        try:
            schedule.time_to = datetime.strptime(data["time_to"], "%H:%M").time()
        except ValueError:
            return {"error": "time_to должен быть в формате HH:MM"}, 400

    if "time_from" in data or "time_to" in data:
        time_from_candidate = schedule.time_from or schedule.start_time
        time_to_candidate = schedule.time_to or schedule.end_time
        if time_from_candidate and time_to_candidate:
            if time_from_candidate >= time_to_candidate:
                return {"error": "time_from must be earlier than time_to"}, 400
            if interval_overlaps_service_break(time_from_candidate, time_to_candidate):
                return {"error": "Selected interval overlaps service break 14:30-15:00"}, 400

    if "status" in data:
        schedule.status = data["status"]
    if "status_comment" in data:
        schedule.status_comment = data["status_comment"]
    if "updated_by" in data:
        schedule.updated_by = data["updated_by"]

    db.commit()
    return format_schedule_v2(schedule)


@bp.route("/schedule/<int:schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    """
    Удаляет занятие
    """
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    
    schedule.status = "cancelled"
    schedule.status_comment = schedule.status_comment or "Отменено"
    db.commit()

    return {"ok": True, "message": "Занятие отменено"}


@bp.route("/schedule/v2/<int:schedule_id>", methods=["DELETE"])
def delete_schedule_v2(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()

    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    schedule.status = "cancelled"
    schedule.status_comment = schedule.status_comment or "Отменено"
    db.commit()

    return {"ok": True, "message": "Занятие отменено"}


@bp.route("/schedule/v2/<int:schedule_id>/cancel", methods=["POST"])
def cancel_schedule_v2(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    object_type = str(schedule.object_type or "").strip().lower()
    if object_type == "rental":
        rental = _load_rental_for_schedule(db, schedule)
        if not rental:
            return {"error": "Аренда не найдена"}, 404

        note = (payload.get("note") or "Отмена аренды зала").strip()
        cancelled_slot = _format_schedule_slot_label(schedule)

        schedule.status = "cancelled"
        schedule.status_comment = (payload.get("status_comment") or f"Отменено: {note}").strip()
        _sync_rental_with_schedule(
            rental,
            status="CANCELLED",
            cancelled=True,
        )

        notify_creator_raw = payload.get("notify_creator", True)
        notify_creator = str(notify_creator_raw).strip().lower() not in {"0", "false", "no", "off"}
        creator_notified = False
        creator_notify_error = None
        if notify_creator:
            creator_notified, creator_notify_error = _notify_rental_creator(
                db,
                rental,
                (
                    "Аренда зала отменена.\n\n"
                    f"Слот: {cancelled_slot}.\n"
                    "Если нужно, свяжитесь со студией для нового времени."
                ),
                context_note="Отмена аренды зала",
            )

        db.commit()
        return jsonify(
            {
                "ok": True,
                "schedule_id": schedule.id,
                "status": schedule.status,
                "creator_notified": creator_notified,
                "creator_notify_error": creator_notify_error,
            }
        )

    if object_type == "individual":
        lesson = _load_individual_lesson_for_schedule(db, schedule)
        if not lesson:
            return {"error": "Индивидуальное занятие не найдено"}, 404

        staff = _get_current_staff(db)
        note = (payload.get("note") or "Отмена индивидуального занятия").strip()
        cancelled_slot = _format_schedule_slot_label(schedule)

        schedule.status = "cancelled"
        schedule.status_comment = (payload.get("status_comment") or f"Отменено: {note}").strip()
        _sync_individual_lesson_with_schedule(
            lesson,
            status="cancelled",
            staff=staff,
        )

        notify_student_raw = payload.get("notify_student", True)
        notify_student = str(notify_student_raw).strip().lower() not in {"0", "false", "no", "off"}
        student_notified = False
        student_notify_error = None
        if notify_student:
            student_notified, student_notify_error = _notify_individual_student(
                db,
                lesson,
                (
                    "Индивидуальное занятие отменено.\n\n"
                    f"Слот: {cancelled_slot}.\n"
                    "Если нужно, свяжитесь со студией для нового времени."
                ),
                context_note="Отмена индивидуального занятия",
            )

        db.commit()
        return jsonify(
            {
                "ok": True,
                "schedule_id": schedule.id,
                "status": schedule.status,
                "student_notified": student_notified,
                "student_notify_error": student_notify_error,
            }
        )

    group_id = _schedule_group_id(schedule)
    if not group_id:
        return {"error": "Отмена по этой механике доступна только для группового занятия"}, 400

    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Группа не найдена"}, 404

    staff = _get_current_staff(db)
    op_reason = f"schedule_cancel:{schedule.id}"
    note = (payload.get("note") or "Отмена занятия с компенсацией группы").strip()

    abonements_by_user = _active_group_abonements_for_schedule_date(db, group_id, schedule.date)
    extended_abonements = 0
    for user_id, abonement in abonements_by_user.items():
        changed = _extend_abonement_by_week(
            db,
            abonement=abonement,
            action_type="schedule_cancel_extend",
            reason=op_reason,
            staff=staff,
            note=note,
            payload={
                "schedule_id": schedule.id,
                "group_id": group_id,
                "user_id": user_id,
                "mode": "cancel",
            },
        )
        if changed:
            extended_abonements += 1

    refunded_credits = 0
    attendance_rows = db.query(Attendance).filter_by(schedule_id=schedule.id).all()
    for attendance in attendance_rows:
        refunded = _refund_schedule_attendance_credit(
            db,
            attendance=attendance,
            action_type="schedule_cancel_refund",
            reason=op_reason,
            staff=staff,
            note="Возврат списания из-за отмены занятия",
            payload={
                "schedule_id": schedule.id,
                "group_id": group_id,
                "user_id": attendance.user_id,
                "mode": "cancel",
            },
        )
        if refunded:
            refunded_credits += 1

    cancelled_slot = _format_schedule_slot_label(schedule)
    schedule.status = "cancelled"
    schedule.status_comment = (payload.get("status_comment") or f"Отменено: {note}").strip()

    notify_group_raw = payload.get("notify_group", True)
    notify_group = str(notify_group_raw).strip().lower() not in {"0", "false", "no", "off"}
    group_notified = False
    group_notify_error = None
    if notify_group:
        group_notified, group_notify_error = _send_group_chat_message(
            group.chat_id,
            f"Извините, занятие на {cancelled_slot} отменилось.",
        )

    db.commit()
    return jsonify(
        {
            "ok": True,
            "schedule_id": schedule.id,
            "status": schedule.status,
            "extended_abonements": extended_abonements,
            "refunded_credits": refunded_credits,
            "group_notified": group_notified,
            "group_notify_error": group_notify_error,
        }
    )


@bp.route("/schedule/v2/<int:schedule_id>/move", methods=["POST"])
def move_schedule_v2(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    object_type = str(schedule.object_type or "").strip().lower()
    if object_type == "rental":
        rental = _load_rental_for_schedule(db, schedule)
        if not rental:
            return {"error": "Аренда не найдена"}, 404

        move_type = str(payload.get("move_type") or "reschedule").strip().lower()
        if move_type not in RENTAL_SCHEDULE_MOVE_TYPE_LABELS:
            return {"error": "move_type должен быть одним из: reschedule"}, 400

        target_date_raw = payload.get("target_date")
        target_time_from_raw = payload.get("target_time_from")
        target_time_to_raw = payload.get("target_time_to")
        if not target_date_raw or not target_time_from_raw or not target_time_to_raw:
            return {"error": "target_date, target_time_from, target_time_to обязательны"}, 400

        try:
            target_date = datetime.strptime(str(target_date_raw), "%Y-%m-%d").date()
            target_time_from = datetime.strptime(str(target_time_from_raw), "%H:%M").time()
            target_time_to = datetime.strptime(str(target_time_to_raw), "%H:%M").time()
        except ValueError:
            return {"error": "Неверный формат даты/времени. Используйте YYYY-MM-DD и HH:MM"}, 400

        if target_time_from >= target_time_to:
            return {"error": "target_time_from должен быть меньше target_time_to"}, 400
        if interval_overlaps_service_break(target_time_from, target_time_to):
            return {"error": "Selected interval overlaps service break 14:30-15:00"}, 400

        if _has_hall_schedule_conflict(
            db,
            schedule_id=schedule.id,
            target_date=target_date,
            target_time_from=target_time_from,
            target_time_to=target_time_to,
        ):
            return {"error": "На выбранный слот зал уже занят"}, 409

        move_label = RENTAL_SCHEDULE_MOVE_TYPE_LABELS[move_type]
        old_slot = _format_schedule_slot_label(schedule)
        new_slot = f"{target_date.strftime('%d.%m.%Y')} {target_time_from.strftime('%H:%M')}–{target_time_to.strftime('%H:%M')}"
        active_status = str(rental.status or schedule.status or "scheduled").strip() or "scheduled"
        if active_status.lower() in {"cancelled", "canceled"}:
            active_status = "scheduled"

        schedule.date = target_date
        schedule.time_from = target_time_from
        schedule.time_to = target_time_to
        schedule.start_time = target_time_from
        schedule.end_time = target_time_to
        schedule.status = active_status
        schedule.status_comment = f"{move_label}: {old_slot} -> {new_slot}"

        _sync_rental_with_schedule(
            rental,
            target_date=target_date,
            target_time_from=target_time_from,
            target_time_to=target_time_to,
            status=active_status,
        )

        notify_creator_raw = payload.get("notify_creator", True)
        notify_creator = str(notify_creator_raw).strip().lower() not in {"0", "false", "no", "off"}
        creator_notified = False
        creator_notify_error = None
        if notify_creator:
            creator_notified, creator_notify_error = _notify_rental_creator(
                db,
                rental,
                (
                    "Аренда зала перенесена.\n\n"
                    f"Было: {old_slot}.\n"
                    f"Теперь: {new_slot}."
                ),
                context_note="Перенос аренды зала",
            )

        db.commit()
        return jsonify(
            {
                "ok": True,
                "schedule": format_schedule_v2(schedule),
                "move_type": move_type,
                "creator_notified": creator_notified,
                "creator_notify_error": creator_notify_error,
            }
        )

    if object_type == "individual":
        lesson = _load_individual_lesson_for_schedule(db, schedule)
        if not lesson:
            return {"error": "Индивидуальное занятие не найдено"}, 404

        move_type = str(payload.get("move_type") or "reschedule").strip().lower()
        if move_type not in INDIVIDUAL_SCHEDULE_MOVE_TYPE_LABELS:
            return {"error": "move_type должен быть одним из: reschedule"}, 400

        target_date_raw = payload.get("target_date")
        target_time_from_raw = payload.get("target_time_from")
        target_time_to_raw = payload.get("target_time_to")
        if not target_date_raw or not target_time_from_raw or not target_time_to_raw:
            return {"error": "target_date, target_time_from, target_time_to обязательны"}, 400

        try:
            target_date = datetime.strptime(str(target_date_raw), "%Y-%m-%d").date()
            target_time_from = datetime.strptime(str(target_time_from_raw), "%H:%M").time()
            target_time_to = datetime.strptime(str(target_time_to_raw), "%H:%M").time()
        except ValueError:
            return {"error": "Неверный формат даты/времени. Используйте YYYY-MM-DD и HH:MM"}, 400

        if target_time_from >= target_time_to:
            return {"error": "target_time_from должен быть меньше target_time_to"}, 400
        if interval_overlaps_service_break(target_time_from, target_time_to):
            return {"error": "Selected interval overlaps service break 14:30-15:00"}, 400

        teacher_id = lesson.teacher_id or schedule.teacher_id
        if not teacher_id:
            return {"error": "У индивидуального занятия не указан преподаватель"}, 400

        if _has_teacher_schedule_conflict(
            db,
            schedule_id=schedule.id,
            teacher_id=teacher_id,
            target_date=target_date,
            target_time_from=target_time_from,
            target_time_to=target_time_to,
            lesson_id=lesson.id,
        ):
            return {"error": "У преподавателя уже есть занятие на выбранный слот"}, 409

        staff = _get_current_staff(db)
        move_label = INDIVIDUAL_SCHEDULE_MOVE_TYPE_LABELS[move_type]
        old_slot = _format_schedule_slot_label(schedule)
        new_slot = f"{target_date.strftime('%d.%m.%Y')} {target_time_from.strftime('%H:%M')}–{target_time_to.strftime('%H:%M')}"
        active_status = str(lesson.status or schedule.status or "scheduled").strip() or "scheduled"
        if active_status.lower() in {"cancelled", "canceled"}:
            active_status = "scheduled"

        schedule.date = target_date
        schedule.time_from = target_time_from
        schedule.time_to = target_time_to
        schedule.start_time = target_time_from
        schedule.end_time = target_time_to
        schedule.status = active_status
        schedule.status_comment = f"{move_label}: {old_slot} -> {new_slot}"

        _sync_individual_lesson_with_schedule(
            lesson,
            target_date=target_date,
            target_time_from=target_time_from,
            target_time_to=target_time_to,
            status=active_status,
            staff=staff,
        )

        notify_student_raw = payload.get("notify_student", True)
        notify_student = str(notify_student_raw).strip().lower() not in {"0", "false", "no", "off"}
        student_notified = False
        student_notify_error = None
        if notify_student:
            student_notified, student_notify_error = _notify_individual_student(
                db,
                lesson,
                (
                    "Индивидуальное занятие перенесено.\n\n"
                    f"Было: {old_slot}.\n"
                    f"Теперь: {new_slot}."
                ),
                context_note="Перенос индивидуального занятия",
            )

        db.commit()
        return jsonify(
            {
                "ok": True,
                "schedule": format_schedule_v2(schedule),
                "move_type": move_type,
                "student_notified": student_notified,
                "student_notify_error": student_notify_error,
            }
        )

    group_id = _schedule_group_id(schedule)
    if not group_id:
        return {"error": "Перенос по этой механике доступен только для группового занятия"}, 400

    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Группа не найдена"}, 404

    move_type = str(payload.get("move_type") or "").strip()
    if move_type not in SCHEDULE_MOVE_TYPE_LABELS:
        return {"error": "move_type должен быть одним из: studio_fault, absence_people, low_attendance"}, 400

    target_date_raw = payload.get("target_date")
    target_time_from_raw = payload.get("target_time_from")
    target_time_to_raw = payload.get("target_time_to")
    if not target_date_raw or not target_time_from_raw or not target_time_to_raw:
        return {"error": "target_date, target_time_from, target_time_to обязательны"}, 400

    try:
        target_date = datetime.strptime(str(target_date_raw), "%Y-%m-%d").date()
        target_time_from = datetime.strptime(str(target_time_from_raw), "%H:%M").time()
        target_time_to = datetime.strptime(str(target_time_to_raw), "%H:%M").time()
    except ValueError:
        return {"error": "Неверный формат даты/времени. Используйте YYYY-MM-DD и HH:MM"}, 400

    if target_time_from >= target_time_to:
        return {"error": "target_time_from должен быть меньше target_time_to"}, 400
    if interval_overlaps_service_break(target_time_from, target_time_to):
        return {"error": "Selected interval overlaps service break 14:30-15:00"}, 400

    if _has_group_schedule_conflict(
        db,
        schedule_id=schedule.id,
        group_id=group_id,
        target_date=target_date,
        target_time_from=target_time_from,
        target_time_to=target_time_to,
    ):
        return {"error": "На выбранный слот уже есть другое занятие этой группы"}, 409

    staff = _get_current_staff(db)
    move_label = SCHEDULE_MOVE_TYPE_LABELS[move_type]
    old_slot = _format_schedule_slot_label(schedule)
    new_slot = f"{target_date.strftime('%d.%m.%Y')} {target_time_from.strftime('%H:%M')}–{target_time_to.strftime('%H:%M')}"
    op_reason = f"schedule_move:{schedule.id}:{move_type}:{target_date.isoformat()}:{target_time_from.strftime('%H:%M')}"

    attendance_rows = db.query(Attendance).filter_by(schedule_id=schedule.id).all()
    abonements_by_user = _active_group_abonements_for_schedule_date(db, group_id, schedule.date)

    extended_abonements = 0
    refunded_credits = 0
    low_attendance_present_count = 0

    if move_type in {"studio_fault", "absence_people"}:
        for user_id, abonement in abonements_by_user.items():
            changed = _extend_abonement_by_week(
                db,
                abonement=abonement,
                action_type="schedule_move_extend",
                reason=op_reason,
                staff=staff,
                note=f"{move_label}: продление на 1 неделю",
                payload={
                    "schedule_id": schedule.id,
                    "group_id": group_id,
                    "user_id": user_id,
                    "move_type": move_type,
                },
            )
            if changed:
                extended_abonements += 1

        for attendance in attendance_rows:
            refunded = _refund_schedule_attendance_credit(
                db,
                attendance=attendance,
                action_type="schedule_move_refund",
                reason=op_reason,
                staff=staff,
                note=f"{move_label}: возврат списания",
                payload={
                    "schedule_id": schedule.id,
                    "group_id": group_id,
                    "user_id": attendance.user_id,
                    "move_type": move_type,
                },
            )
            if refunded:
                refunded_credits += 1
    else:
        present_rows = [
            row for row in attendance_rows
            if (row.status or "").strip().lower() in SCHEDULE_PRESENT_STATUSES
        ]
        low_attendance_present_count = len(present_rows)
        if low_attendance_present_count >= 3:
            return {
                "error": "Для типа 'Нехватка людей' количество пришедших должно быть меньше 3",
                "present_count": low_attendance_present_count,
            }, 400

        for attendance in present_rows:
            abonement = None
            if attendance.abonement_id:
                abonement = db.query(GroupAbonement).filter_by(id=attendance.abonement_id).first()
            if not abonement:
                abonement = abonements_by_user.get(attendance.user_id)
                if abonement and not attendance.abonement_id:
                    attendance.abonement_id = abonement.id

            if abonement:
                changed = _extend_abonement_by_week(
                    db,
                    abonement=abonement,
                    action_type="schedule_move_present_extend",
                    reason=op_reason,
                    staff=staff,
                    note="Нехватка людей: продление на 1 неделю пришедшему ученику",
                    payload={
                        "schedule_id": schedule.id,
                        "group_id": group_id,
                        "user_id": attendance.user_id,
                        "move_type": move_type,
                    },
                )
                if changed:
                    extended_abonements += 1

            refunded = _refund_schedule_attendance_credit(
                db,
                attendance=attendance,
                action_type="schedule_move_present_refund",
                reason=op_reason,
                staff=staff,
                note="Нехватка людей: добавлено 1 занятие пришедшему ученику",
                payload={
                    "schedule_id": schedule.id,
                    "group_id": group_id,
                    "user_id": attendance.user_id,
                    "move_type": move_type,
                },
            )
            if refunded:
                refunded_credits += 1

    schedule.date = target_date
    schedule.time_from = target_time_from
    schedule.time_to = target_time_to
    schedule.start_time = target_time_from
    schedule.end_time = target_time_to
    schedule.status = "scheduled"
    schedule.status_comment = f"{move_label}: {old_slot} -> {new_slot}"

    notify_group_raw = payload.get("notify_group", True)
    notify_group = str(notify_group_raw).strip().lower() not in {"0", "false", "no", "off"}
    group_notified = False
    group_notify_error = None
    if notify_group:
        group_notified, group_notify_error = _send_group_chat_message(
            group.chat_id,
            f"Занятие перенесено ({move_label}). Было: {old_slot}. Теперь: {new_slot}.",
        )

    db.commit()
    return jsonify(
        {
            "ok": True,
            "schedule": format_schedule_v2(schedule),
            "move_type": move_type,
            "extended_abonements": extended_abonements,
            "refunded_credits": refunded_credits,
            "present_count": low_attendance_present_count if move_type == "low_attendance" else None,
            "group_notified": group_notified,
            "group_notify_error": group_notify_error,
            "attendance_rows_total": len(attendance_rows),
        }
    )

# -------------------- ATTENDANCE --------------------

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


def _serialize_user_payload(user: User, *, include_photo: bool = False) -> dict:
    payload = {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes,
    }
    if include_photo:
        payload["photo_path"] = user.photo_path
    return payload


def _build_staff_check_payload(db, telegram_id: int) -> dict:
    user = None
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
    except Exception:
        user = None

    staff = None
    if user:
        staff = db.query(Staff).filter_by(user_id=user.id, status="active").first()
    if not staff:
        staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff:
        return {"is_staff": False, "staff": None}

    # Load user profile to fill possible missing staff fields.
    if not user and staff.user_id:
        user = db.query(User).filter_by(id=staff.user_id).first()

    can_edit, edit_block_reason = _staff_editability_payload(db, staff)
    staff_data = {
        "id": staff.id,
        "name": staff.name or (user.name if user else None),
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "teaches": staff.teaches,
        "phone": staff.phone,
        "email": staff.email,
        "photo_path": staff.photo_path or (user.photo_path if user else None),
        "can_edit": can_edit,
        "can_delete": can_edit,
        "edit_block_reason": edit_block_reason,
    }
    return {"is_staff": True, "staff": staff_data}


@bp.route("/users", methods=["POST"])
def register_user():
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}

    if not data.get("name"):
        return {"error": "name is required"}, 400

    telegram_id_raw = data.get("telegram_id")
    telegram_id = None
    if telegram_id_raw not in (None, ""):
        try:
            telegram_id = int(telegram_id_raw)
        except (TypeError, ValueError):
            return {"error": "telegram_id must be an integer"}, 400

    if telegram_id is not None:
        existing_user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if existing_user:
            return {"error": "user with this telegram_id already exists"}, 409

    user = User(
        telegram_id=telegram_id,
        username=data.get("username"),
        phone=data.get("phone"),
        name=data["name"],
        email=data.get("email"),
        birth_date=datetime.strptime(data["birth_date"], "%Y-%m-%d").date() if data.get("birth_date") else None,
        user_notes=data.get("user_notes"),
        staff_notes=data.get("staff_notes")
    )
    db.add(user)
    db.commit()

    return _serialize_user_payload(user), 201


@bp.route("/users/self", methods=["POST"])
def register_user_self():
    db = g.db

    telegram_id = getattr(g, "telegram_id", None)
    if not telegram_id:
        return {"error": "auth required"}, 401
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return {"error": "invalid telegram_id"}, 400

    existing_user = db.query(User).filter_by(telegram_id=telegram_id).first()
    if existing_user:
        return _serialize_user_payload(existing_user), 200

    data = request.json or {}
    user = User(
        telegram_id=telegram_id,
        username=data.get("username"),
        phone=data.get("phone"),
        name=(data.get("name") or "Пользователь").strip() or "Пользователь",
        email=data.get("email"),
        birth_date=datetime.strptime(data["birth_date"], "%Y-%m-%d").date() if data.get("birth_date") else None,
        user_notes=data.get("user_notes"),
    )
    db.add(user)
    db.commit()
    return _serialize_user_payload(user), 201


@bp.route("/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    return _serialize_user_payload(user)


@bp.route("/users/me", methods=["GET"])
def get_my_user():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "user not found"}, 404
    payload = _serialize_user_payload(user)

    identities = db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).all()
    provider_map = {}
    for identity in identities:
        if identity.provider not in {"telegram", "vk"}:
            continue
        provider_map[identity.provider] = {
            "id": identity.provider_user_id,
            "username": identity.provider_username,
        }
    if provider_map:
        payload["auth_providers"] = provider_map

    if user.telegram_id is not None:
        staff = db.query(Staff).filter_by(user_id=user.id, status="active").first()
        if not staff and user.telegram_id is not None:
            staff = db.query(Staff).filter_by(telegram_id=user.telegram_id, status="active").first()
        if staff:
            photo_path = staff.photo_path or user.photo_path
            if photo_path:
                payload["photo_path"] = photo_path

    return payload


@bp.route("/users/list/all")
def list_all_users():
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error
    db = g.db
    users = db.query(User).order_by(User.registered_at.desc()).all()
    
    return jsonify([
        {
            "id": u.id,
            "telegram_id": u.telegram_id,
            "username": u.username,
            "phone": u.phone,
            "name": u.name,
            "email": u.email,
            "birth_date": u.birth_date.isoformat() if u.birth_date else None,
            "registered_at": u.registered_at.isoformat(),
            "status": u.status,
            "user_notes": u.user_notes,
            "staff_notes": u.staff_notes
        } for u in users
    ])


@bp.route("/users/<int:user_id>", methods=["PUT"])
def update_user(user_id):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    data = request.json or {}
    
    if "phone" in data:
        user.phone = data["phone"]
    if "name" in data:
        user.name = data["name"]
    if "email" in data:
        user.email = data["email"]
    if "birth_date" in data and data["birth_date"]:
        user.birth_date = datetime.strptime(data["birth_date"], "%Y-%m-%d").date()
    if "status" in data:
        user.status = data["status"]
    if "user_notes" in data:
        user.user_notes = data["user_notes"]
    if "staff_notes" in data:
        user.staff_notes = data["staff_notes"]
    
    db.commit()
    
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes,
        "photo_path": user.photo_path
    }


@bp.route("/staff")
def get_all_staff():
    """
    Получить всех сотрудников
    """
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter_by(status="active").order_by(Staff.created_at.desc()).all()
    
    result = []
    for s in staff:
        # Получаем username из User если есть telegram_id
        username = None
        if s.telegram_id:
            user = db.query(User).filter_by(telegram_id=s.telegram_id).first()
            if user:
                username = user.username
        can_edit, edit_block_reason = _staff_editability_payload(db, s)
        
        result.append({
            "id": s.id,
            "name": s.name,
            "phone": s.phone,
            "email": s.email,
            "telegram_id": s.telegram_id,
            "username": username,
            "position": s.position,
            "specialization": s.specialization,
            "bio": s.bio,
            "photo_path": s.photo_path,
            "teaches": s.teaches,
            "status": s.status,
            "created_at": s.created_at.isoformat(),
            "can_edit": can_edit,
            "can_delete": can_edit,
            "edit_block_reason": edit_block_reason,
        })
    
    return jsonify(result)


@bp.route("/staff/check/<int:telegram_id>")
def check_staff_by_telegram(telegram_id):
    """
    Проверить является ли пользователем сотрудником.
    Если данные персонала неполные, подгружает данные из профиля пользователя (без сохранения в БД).
    """
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error

    try:
        db = g.db
        return jsonify(_build_staff_check_payload(db, telegram_id))
    except Exception as e:
        print(f"⚠️ Ошибка при проверке сотрудника: {e}")
        return jsonify({
            "is_staff": False,
            "staff": None
        })


@bp.route("/staff/me")
def check_current_staff():
    telegram_id = getattr(g, "telegram_id", None)
    if not telegram_id:
        return {"error": "auth required"}, 401
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return {"error": "invalid telegram_id"}, 400

    try:
        db = g.db
        return jsonify(_build_staff_check_payload(db, telegram_id))
    except Exception as e:
        print(f"⚠️ Ошибка при проверке сотрудника: {e}")
        return jsonify({
            "is_staff": False,
            "staff": None
        })


@bp.route("/staff", methods=["POST"])
def create_staff():
    """
    Создать новый профиль сотрудника.
    Обязательные поля: position, name (или telegram_id с профилем)
    Остальные опциональные.
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}

    telegram_id = data.get("telegram_id")
    staff_user = None
    if telegram_id is not None:
        staff_user = db.query(User).filter_by(telegram_id=telegram_id).first()

    # Получаем имя: либо из данных, либо из профиля пользователя
    staff_name = data.get("name")
    if not staff_name and staff_user and staff_user.name:
        staff_name = staff_user.name
    
    if not staff_name or not data.get("position"):
        return {"error": "name (или telegram_id с профилем) и position обязательны"}, 400

    normalized_position = _normalize_staff_role(data.get("position"))

    # Проверяем допустимые должности
    valid_positions = ["учитель", "модератор", "администратор", "старший админ", "владелец", "тех. админ"]
    if normalized_position not in valid_positions:
        return {"error": f"Допустимые должности: {', '.join(valid_positions)}"}, 400
    role_assign_error = _staff_assignment_guard(db, normalized_position)
    if role_assign_error:
        return role_assign_error
    data["position"] = normalized_position

    notify_flag = data.get("notify", True)
    notify_user = str(notify_flag).strip().lower() in ["1", "true", "yes", "y", "on"]

    teaches_value = 0
    teaches_raw = normalize_teaches(data.get("teaches"))
    if teaches_raw is None:
        teaches_value = 1 if data.get("position") == "учитель" else 0
    else:
        teaches_value = teaches_raw

    
    # Защита от дублей по user_id / telegram_id
    existing_staff = None
    if staff_user:
        existing_staff = db.query(Staff).filter_by(user_id=staff_user.id).first()
    if not existing_staff and telegram_id is not None:
        existing_staff = db.query(Staff).filter_by(telegram_id=telegram_id).first()
    if existing_staff:
        if existing_staff.status == "dismissed":
            existing_staff.name = staff_name
            existing_staff.position = data["position"]
            existing_staff.specialization = data.get("specialization")
            existing_staff.bio = data.get("bio")
            existing_staff.status = "active"
            existing_staff.teaches = teaches_value
            if staff_user:
                existing_staff.user_id = staff_user.id
            if telegram_id is not None:
                existing_staff.telegram_id = telegram_id
            db.commit()

            if telegram_id is not None:
                try_fetch_telegram_avatar(telegram_id, db, staff_obj=existing_staff)

            if telegram_id is not None and notify_user:
                try:
                    import requests
                    from dance_studio.core.config import BOT_TOKEN

                    position_display = {
                        "учитель": "👩‍🏫 Учитель",
                        "администратор": "📋 Администратор",
                        "старший админ": "🛡️ Старший админ",
                        "владелец": "👑 Владелец",
                        "тех. админ": "⚙️ Технический администратор"
                    }

                    position_name = position_display.get(data["position"], data["position"])
                    message_text = (
                        f"🎉 Вы снова в команде!\n\n"
                        f"Вам назначена должность:\n"
                        f"<b>{position_name}</b>\n\n"
                        f"Добро пожаловать обратно!"
                    )

                    send_user_notification_sync(
                        user_id=data.get("telegram_id"),
                        text=message_text,
                        context_note="Восстановление сотрудника"
                    )
                except Exception:
                    pass

                return {
                    "message": "Персонал восстановлен",
                    "id": existing_staff.id,
                    "restored": True
                }, 200

            return {
                "error": "Пользователь с таким telegram_id уже существует",
                "existing_id": existing_staff.id
            }, 409
    
    staff = Staff(
        name=staff_name,
        phone=data.get("phone") or "+7 000 000 00 00",  # Телефон опциональный
        email=data.get("email"),
        telegram_id=telegram_id,
        user_id=staff_user.id if staff_user else None,
        position=data["position"],
        specialization=data.get("specialization"),
        bio=data.get("bio"),
        teaches=teaches_value,
        status=data.get("status", "active")
    )
    db.add(staff)
    db.commit()

    if data.get("telegram_id"):
        try_fetch_telegram_avatar(data.get("telegram_id"), db, staff_obj=staff)
    
    # Отправляем уведомление в Telegram если есть telegram_id
    if data.get("telegram_id") and notify_user:
        try:
            import requests
            from dance_studio.core.config import BOT_TOKEN
            
            position_display = {
                "учитель": "👩‍🏫 Учитель",
                "администратор": "📋 Администратор",
                "старший админ": "🛡️ Старший админ",
                "владелец": "👑 Владелец",
                "тех. админ": "⚙️ Технический администратор"
            }
            
            position_name = position_display.get(data["position"], data["position"])
            
            message_text = (
                f"🎉 Поздравляем!\n\n"
                f"Вы назначены на должность:\n"
                f"<b>{position_name}</b>\n\n"
                f"в студии танца {PROJECT_NAME_FULL}!"
            )
            
            send_user_notification_sync(
                user_id=data.get("telegram_id"),
                text=message_text,
                context_note="Назначение сотрудника"
            )
        except Exception:
            pass
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": staff.photo_path,
        "status": staff.status,
        "created_at": staff.created_at.isoformat()
    }, 201


@bp.route("/staff/<int:staff_id>", methods=["GET"])
def get_staff(staff_id):
    """
    Получить информацию о сотруднике
    """
    perm_error = require_permission("manage_staff", allow_self_staff_id=staff_id)
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404

    username = None
    photo_path = staff.photo_path
    if staff.telegram_id:
        user = db.query(User).filter_by(telegram_id=staff.telegram_id).first()
        if user:
            username = user.username
            if not photo_path and user.photo_path:
                photo_path = user.photo_path
    can_edit, edit_block_reason = _staff_editability_payload(db, staff)
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "username": username,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": photo_path,
        "teaches": staff.teaches,
        "status": staff.status,
        "created_at": staff.created_at.isoformat(),
        "can_edit": can_edit,
        "can_delete": can_edit,
        "edit_block_reason": edit_block_reason,
    }


@bp.route("/staff/update-from-telegram/<int:telegram_id>", methods=["PUT"])
def update_staff_from_telegram(telegram_id):
    """
    Обновляет имя и другие данные персонала из Telegram профиля
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}
    
    staff = db.query(Staff).filter_by(telegram_id=telegram_id).first()
    
    if not staff:
        return {"error": "Персонал не найден"}, 404
    
    if "first_name" in data:
        # Формируем полное имя из first_name и last_name
        name = data["first_name"]
        if data.get("last_name"):
            name += " " + data["last_name"]
        staff.name = name
    
    db.commit()
    
    return {
        "id": staff.id,
        "name": staff.name,
        "position": staff.position,
        "message": "Имя обновлено из Telegram"
    }


@bp.route("/staff/<int:staff_id>", methods=["PUT"])
def update_staff(staff_id):
    """
    Обновить информацию о сотруднике
    """
    perm_error = require_permission("manage_staff", allow_self_staff_id=staff_id)
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    edit_guard_error = _staff_edit_guard(db, staff)
    if edit_guard_error:
        return edit_guard_error
    
    data = request.json or {}
    
    if "name" in data:
        staff.name = data["name"]
    if "phone" in data:
        staff.phone = data["phone"]
    if "email" in data:
        staff.email = data["email"]
    if "telegram_id" in data:
        staff.telegram_id = data["telegram_id"]
    if "position" in data:
        position_perm_error = require_permission("manage_staff")
        if position_perm_error:
            return position_perm_error
        valid_positions = {"учитель", "администратор", "старший админ", "модератор", "владелец", "тех. админ"}
        normalized_position = _normalize_staff_role(data["position"])
        if normalized_position not in valid_positions:
            return {"error": f"Допустимые должности: {', '.join(valid_positions)}"}, 400
        role_assign_error = _staff_assignment_guard(db, normalized_position)
        if role_assign_error:
            return role_assign_error
        staff.position = normalized_position
    if "specialization" in data:
        staff.specialization = data["specialization"]
    if "bio" in data:
        staff.bio = data["bio"]
    if "teaches" in data:
        allowed_positions = {"администратор", "старший админ", "владелец", "тех. админ"}
        actor_position = _resolve_actor_staff_role(db)
        if actor_position not in allowed_positions:
            return {"error": "Нет прав на изменение поля teaches"}, 403
        staff.teaches = normalize_teaches(data["teaches"])
    if "status" in data:
        staff.status = data["status"]
    
    db.commit()
    
    can_edit, edit_block_reason = _staff_editability_payload(db, staff)
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": staff.photo_path,
        "teaches": staff.teaches,
        "status": staff.status,
        "created_at": staff.created_at.isoformat(),
        "can_edit": can_edit,
        "can_delete": can_edit,
        "edit_block_reason": edit_block_reason,
    }


@bp.route("/teacher-working-hours/<int:teacher_id>", methods=["GET"])
def get_teacher_working_hours(teacher_id):
    perm_error = require_permission("manage_staff", allow_self_staff_id=teacher_id)
    if perm_error:
        return perm_error

    db = g.db
    items = (
        db.query(TeacherWorkingHours)
        .filter_by(teacher_id=teacher_id, status="active")
        .order_by(TeacherWorkingHours.weekday.asc(), TeacherWorkingHours.time_from.asc())
        .all()
    )
    return [
        {
            "id": i.id,
            "teacher_id": i.teacher_id,
            "weekday": i.weekday,
            "time_from": i.time_from.strftime("%H:%M") if i.time_from else None,
            "time_to": i.time_to.strftime("%H:%M") if i.time_to else None,
            "valid_from": i.valid_from.isoformat() if i.valid_from else None,
            "valid_to": i.valid_to.isoformat() if i.valid_to else None,
            "status": i.status,
            "created_at": i.created_at.isoformat() if i.created_at else None,
            "updated_at": i.updated_at.isoformat() if i.updated_at else None
        }
        for i in items
    ]


@bp.route("/api/stats/teacher", methods=["GET"])
def get_teacher_stats():
    perm_error = require_permission("view_stats")
    if perm_error:
        return perm_error

    db = g.db
    try:
        teacher_id = int(request.args.get("teacher_id", 0))
    except (TypeError, ValueError):
        return {"error": "teacher_id должен быть числом"}, 400
    if not teacher_id:
        return {"error": "teacher_id обязателен"}, 400

    try:
        date_from_val, date_to_val = _parse_stats_date_range(
            request.args.get("date_from"),
            request.args.get("date_to"),
        )
    except ValueError as exc:
        return {"error": str(exc)}, 400

    schedules_q = db.query(Schedule).filter(
        Schedule.teacher_id == teacher_id,
        Schedule.status != "cancelled"
    )
    if date_from_val:
        schedules_q = schedules_q.filter(Schedule.date >= date_from_val)
    if date_to_val:
        schedules_q = schedules_q.filter(Schedule.date <= date_to_val)

    schedules = schedules_q.all()
    schedule_ids = [s.id for s in schedules]

    stats = {
        "teacher_id": teacher_id,
        "date_from": date_from_val.isoformat() if date_from_val else None,
        "date_to": date_to_val.isoformat() if date_to_val else None,
        "lessons_count": len(schedules),
        "students_total": 0,
        "present": 0,
        "absent": 0,
        "late": 0,
        "sick": 0,
    }

    if schedule_ids:
        attendance_rows = db.query(Attendance).filter(Attendance.schedule_id.in_(schedule_ids)).all()
        for row in attendance_rows:
            status = row.status or "absent"
            if status == "sick":
                stats["sick"] += 1
                continue
            stats["students_total"] += 1
            if status == "present":
                stats["present"] += 1
            elif status == "late":
                stats["late"] += 1
            else:
                stats["absent"] += 1

    return jsonify(stats)


@bp.route("/api/stats/studio", methods=["GET"])
def get_studio_stats():
    perm_error = require_permission("view_stats")
    if perm_error:
        return perm_error

    db = g.db
    try:
        date_from_val, date_to_val = _parse_stats_date_range(
            request.args.get("date_from"),
            request.args.get("date_to"),
        )
    except ValueError as exc:
        return {"error": str(exc)}, 400

    start_dt, end_dt = _stats_datetime_bounds(date_from_val, date_to_val)

    stats = {
        "date_from": date_from_val.isoformat() if date_from_val else None,
        "date_to": date_to_val.isoformat() if date_to_val else None,
        "new_clients": 0,
        "active_clients": 0,
        "abonements_sold": 0,
        "visits_total": 0,
        "lesson_cancellations": 0,
        "lesson_cancellations_by_type": {"group": 0, "individual": 0, "rental": 0, "other": 0},
        "booking_requests_created": 0,
        "booking_requests_by_type": {"group": 0, "individual": 0, "rental": 0},
        "expected_revenue_rub": 0,
        "expected_revenue_breakdown_rub": {"group": 0, "individual": 0, "rental": 0},
        "pricing_snapshot": {
            "individual_hour_price_rub": _safe_int_setting_value(db, "individual.base_hour_price_rub"),
            "rental_hour_price_rub": _safe_int_setting_value(db, "rental.base_hour_price_rub"),
        },
    }

    users_q = db.query(User)
    if start_dt:
        users_q = users_q.filter(User.registered_at >= start_dt)
    if end_dt:
        users_q = users_q.filter(User.registered_at <= end_dt)
    stats["new_clients"] = users_q.count()

    created_requests_q = db.query(BookingRequest)
    if start_dt:
        created_requests_q = created_requests_q.filter(BookingRequest.created_at >= start_dt)
    if end_dt:
        created_requests_q = created_requests_q.filter(BookingRequest.created_at <= end_dt)
    created_requests = created_requests_q.all()
    stats["booking_requests_created"] = len(created_requests)

    for booking in created_requests:
        object_type = str(booking.object_type or "").strip().lower()
        if object_type in stats["booking_requests_by_type"]:
            stats["booking_requests_by_type"][object_type] += 1
        if object_type != "group" or str(booking.status or "").strip().lower() in NEGATIVE_BOOKING_STATUSES:
            continue
        amount = _booking_expected_amount_rub(db, booking)
        if amount:
            stats["expected_revenue_breakdown_rub"]["group"] += amount

    paid_group_bookings = (
        db.query(BookingRequest)
        .filter(
            BookingRequest.object_type == "group",
            BookingRequest.status.in_(list(BOOKING_PAYMENT_CONFIRMED_STATUSES)),
        )
        .all()
    )
    paid_group_total = 0
    for booking in paid_group_bookings:
        paid_at = booking.status_updated_at or booking.created_at
        if start_dt and (not paid_at or paid_at < start_dt):
            continue
        if end_dt and (not paid_at or paid_at > end_dt):
            continue
        paid_group_total += 1
    stats["abonements_sold"] = paid_group_total

    attendance_q = (
        db.query(Attendance.user_id, Attendance.status)
        .join(Schedule, Attendance.schedule_id == Schedule.id)
        .filter(Schedule.date.isnot(None))
    )
    if date_from_val:
        attendance_q = attendance_q.filter(Schedule.date >= date_from_val)
    if date_to_val:
        attendance_q = attendance_q.filter(Schedule.date <= date_to_val)
    attendance_rows = attendance_q.all()

    active_user_ids: set[int] = set()
    visits_total = 0
    for user_id, status in attendance_rows:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in SCHEDULE_PRESENT_STATUSES:
            continue
        if user_id:
            active_user_ids.add(int(user_id))
        visits_total += 1

    non_group_bookings_q = (
        db.query(BookingRequest)
        .filter(
            BookingRequest.object_type.in_(["individual", "rental"]),
            BookingRequest.status.in_(list(ACTIVE_NON_GROUP_BOOKING_STATUSES)),
            BookingRequest.date.isnot(None),
        )
    )
    if date_from_val:
        non_group_bookings_q = non_group_bookings_q.filter(BookingRequest.date >= date_from_val)
    if date_to_val:
        non_group_bookings_q = non_group_bookings_q.filter(BookingRequest.date <= date_to_val)
    non_group_bookings = non_group_bookings_q.all()

    for booking in non_group_bookings:
        if booking.user_id:
            active_user_ids.add(int(booking.user_id))
        visits_total += 1
        object_type = str(booking.object_type or "").strip().lower()
        if object_type not in stats["expected_revenue_breakdown_rub"]:
            continue
        amount = _booking_expected_amount_rub(db, booking)
        if amount:
            stats["expected_revenue_breakdown_rub"][object_type] += amount

    stats["active_clients"] = len(active_user_ids)
    stats["visits_total"] = visits_total

    cancelled_schedules_q = (
        db.query(Schedule.object_type)
        .filter(
            Schedule.date.isnot(None),
            Schedule.status.in_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
    )
    if date_from_val:
        cancelled_schedules_q = cancelled_schedules_q.filter(Schedule.date >= date_from_val)
    if date_to_val:
        cancelled_schedules_q = cancelled_schedules_q.filter(Schedule.date <= date_to_val)
    cancelled_schedule_rows = cancelled_schedules_q.all()

    stats["lesson_cancellations"] = len(cancelled_schedule_rows)
    for (object_type,) in cancelled_schedule_rows:
        normalized_type = str(object_type or "").strip().lower()
        if normalized_type in stats["lesson_cancellations_by_type"]:
            stats["lesson_cancellations_by_type"][normalized_type] += 1
        else:
            stats["lesson_cancellations_by_type"]["other"] += 1

    stats["expected_revenue_rub"] = sum(stats["expected_revenue_breakdown_rub"].values())
    return jsonify(stats)


@bp.route("/teacher-working-hours/<int:teacher_id>", methods=["PUT"])
def put_teacher_working_hours(teacher_id):
    perm_error = require_permission("manage_staff", allow_self_staff_id=teacher_id)
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}
    items = data.get("items", [])
    if not isinstance(items, list):
        return {"error": "items должен быть списком"}, 400

    parsed_items = []
    for item in items:
        try:
            weekday = int(item.get("weekday"))
        except (TypeError, ValueError):
            return {"error": "weekday должен быть числом 0..6"}, 400
        if weekday < 0 or weekday > 6:
            return {"error": "weekday должен быть в диапазоне 0..6"}, 400

        time_from_str = item.get("time_from")
        time_to_str = item.get("time_to")
        if not time_from_str or not time_to_str:
            return {"error": "time_from и time_to обязательны"}, 400
        try:
            time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
            time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
        except ValueError:
            return {"error": "time_from и time_to должны быть в формате HH:MM"}, 400
        if time_from_val >= time_to_val:
            return {"error": "time_from должен быть меньше time_to"}, 400

        valid_from = item.get("valid_from")
        valid_to = item.get("valid_to")
        try:
            valid_from_val = datetime.strptime(valid_from, "%Y-%m-%d").date() if valid_from else None
            valid_to_val = datetime.strptime(valid_to, "%Y-%m-%d").date() if valid_to else None
        except ValueError:
            return {"error": "valid_from и valid_to должны быть в формате YYYY-MM-DD"}, 400

        parsed_items.append(
            {
                "weekday": weekday,
                "time_from": time_from_val,
                "time_to": time_to_val,
                "valid_from": valid_from_val,
                "valid_to": valid_to_val,
            }
        )

    existing = db.query(TeacherWorkingHours).filter_by(teacher_id=teacher_id, status="active").all()
    for row in existing:
        row.status = "archived"
        row.updated_at = datetime.now()

    for item in parsed_items:
        db.add(
            TeacherWorkingHours(
                teacher_id=teacher_id,
                weekday=item["weekday"],
                time_from=item["time_from"],
                time_to=item["time_to"],
                valid_from=item["valid_from"],
                valid_to=item["valid_to"],
                status="active",
            )
        )

    db.commit()

    return {
        "items": [
            {
                "weekday": i["weekday"],
                "time_from": i["time_from"].strftime("%H:%M"),
                "time_to": i["time_to"].strftime("%H:%M"),
                "valid_from": i["valid_from"].isoformat() if i["valid_from"] else None,
                "valid_to": i["valid_to"].isoformat() if i["valid_to"] else None,
                "status": "active",
            }
            for i in parsed_items
        ]
    }


@bp.route("/staff/<int:staff_id>", methods=["DELETE"])
def delete_staff(staff_id):
    """
    Удалить сотрудника
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    edit_guard_error = _staff_edit_guard(db, staff)
    if edit_guard_error:
        return edit_guard_error
    
    staff_name = staff.name
    telegram_id = staff.telegram_id

    # Вместо физического удаления — деактивируем, чтобы не ломать расписание
    staff.status = "dismissed"
    staff.teaches = 0
    db.commit()
    
    notify_flag = request.args.get("notify", "1").strip().lower()
    notify_user = notify_flag in ["1", "true", "yes", "y", "on"]

    # Отправляем уведомление об увольнении в Telegram если есть telegram_id
    if telegram_id and notify_user:
        try:
            import requests
            from dance_studio.core.config import BOT_TOKEN
            
            message_text = (
                f" К сожалению...\n\n"
                f"Вы удалены из персонала студии танца {PROJECT_NAME_FULL}.\n\n"
                f"Спасибо за сотрудничество!"
            )
            
            send_user_notification_sync(
                user_id=telegram_id,
                text=message_text,
                context_note="Увольнение сотрудника"
            )
        except Exception:
            pass
    
    return {
        "message": f"Персонал '{staff_name}' удален",
        "deleted_id": staff_id,
        "status": staff.status
    }


@bp.route("/staff/<int:staff_id>/photo", methods=["POST"])
def upload_staff_photo(staff_id):
    """
    Загружает фото сотрудника
    """
    perm_error = require_permission("manage_staff", allow_self_staff_id=staff_id)
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    edit_guard_error = _staff_edit_guard(db, staff)
    if edit_guard_error:
        return edit_guard_error
    
    if 'photo' not in request.files:
        return {"error": "Файл не предоставлен"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Файл не выбран"}, 400
    
    # Проверяем расширение
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Допустимые форматы: jpg, jpeg, png, gif"}, 400
    
    try:
        # Удаляем старое фото если существует
        if staff.photo_path:
            delete_user_photo(staff.photo_path)
        
        # Сохраняем новое фото в папку teachers
        file_data = file.read()
        filename = "photo." + file.filename.rsplit('.', 1)[1].lower()
        
        from dance_studio.core.media_manager import TEACHERS_MEDIA_DIR
        staff_dir = os.path.join(TEACHERS_MEDIA_DIR, str(staff_id))
        os.makedirs(staff_dir, exist_ok=True)
        
        file_path = os.path.join(staff_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        photo_path = os.path.relpath(file_path, BASE_DIR)
        
        staff.photo_path = photo_path
        db.commit()
        
        return {
            "id": staff.id,
            "photo_path": staff.photo_path,
            "message": "Фото успешно загружено"
        }, 201
    
    except Exception:
        return internal_server_error_response(
            context="Failed to upload staff photo",
            db=db,
        )


@bp.route("/staff/<int:staff_id>/photo", methods=["DELETE"])
def delete_staff_photo(staff_id):
    """
    Удаляет фото сотрудника
    """
    perm_error = require_permission("manage_staff", allow_self_staff_id=staff_id)
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    edit_guard_error = _staff_edit_guard(db, staff)
    if edit_guard_error:
        return edit_guard_error
    
    if not staff.photo_path:
        return {"error": "Фото не найдено"}, 404
    
    try:
        delete_user_photo(staff.photo_path)
        staff.photo_path = None
        db.commit()
        
        return {"ok": True, "message": "Фото удалено"}
    
    except Exception:
        return internal_server_error_response(
            context="Failed to delete staff photo",
            db=db,
        )


@bp.route("/api/teachers", methods=["GET"])
def list_public_teachers():
    db = g.db
    teachers = db.query(Staff).filter(
        Staff.status == "active",
        or_(
            Staff.teaches == 1,
            (Staff.position.in_(["учитель", "Учитель"]) & Staff.teaches.is_(None))
        )
    ).all()

    return jsonify([
        {
            "id": t.id,
            "name": t.name,
            "position": t.position,
            "specialization": t.specialization,
            "bio": t.bio,
            "photo": t.photo_path,
        }
        for t in teachers
    ])


@bp.route("/api/teachers/<int:teacher_id>", methods=["GET"])
def get_public_teacher(teacher_id):
    db = g.db
    teacher = (
        db.query(Staff)
        .filter(
            Staff.id == teacher_id,
            Staff.status == "active",
            or_(
                Staff.teaches == 1,
                (Staff.position.in_(["учитель", "Учитель"]) & Staff.teaches.is_(None))
            )
        )
        .first()
    )
    if not teacher:
        return {"error": "Преподаватель не найден"}, 404
    teacher_username = None
    contact_link = None
    if teacher.telegram_id:
        teacher_user = db.query(User).filter_by(telegram_id=teacher.telegram_id).first()
        teacher_username = (getattr(teacher_user, "username", None) or "").strip() or None
        if teacher_username:
            normalized_username = teacher_username[1:] if teacher_username.startswith("@") else teacher_username
            if normalized_username:
                contact_link = f"https://t.me/{normalized_username}"
        if not contact_link:
            contact_link = f"tg://user?id={teacher.telegram_id}"

    groups = (
        db.query(Group)
        .filter(Group.teacher_id == teacher.id)
        .order_by(Group.created_at.desc())
        .all()
    )
    occupancy_map = get_group_occupancy_map(db, [group.id for group in groups if group and group.id])
    group_items = []
    for group in groups:
        direction = db.query(Direction).filter(Direction.direction_id == group.direction_id).first()
        occupied_students = int(occupancy_map.get(int(group.id), 0))
        try:
            max_students = int(group.max_students or 0)
        except (TypeError, ValueError):
            max_students = 0
        free_seats = max(0, max_students - occupied_students) if max_students > 0 else None
        group_items.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "age_group": group.age_group,
            "duration_minutes": group.duration_minutes,
            "lessons_per_week": group.lessons_per_week,
            "max_students": group.max_students,
            "occupied_students": occupied_students,
            "free_seats": free_seats,
            "direction_id": direction.direction_id if direction else group.direction_id,
            "direction_title": direction.title if direction else None,
            "direction_type": direction.direction_type if direction else None,
            "direction_status": direction.status if direction else None,
            "direction_image": _build_image_url(direction.image_path) if direction else None,
        })
    return {
        "id": teacher.id,
        "name": teacher.name,
        "position": teacher.position,
        "specialization": teacher.specialization,
        "bio": teacher.bio,
        "photo": teacher.photo_path,
        "username": teacher_username,
        "contact_link": contact_link,
        "groups": group_items,
    }


@bp.route("/api/teachers/<int:teacher_id>/schedule", methods=["GET"])
def get_public_teacher_schedule(teacher_id):
    db = g.db
    teacher_exists = db.query(Staff).filter(
        Staff.id == teacher_id,
        Staff.status == "active",
        or_(
            Staff.teaches == 1,
            (Staff.position.in_(["учитель", "Учитель"]) & Staff.teaches.is_(None))
        )
    ).first()
    if not teacher_exists:
        return {"error": "Преподаватель не найден"}, 404
    items = (
        db.query(TeacherWorkingHours)
        .filter_by(teacher_id=teacher_id, status="active")
        .order_by(TeacherWorkingHours.weekday.asc(), TeacherWorkingHours.time_from.asc())
        .all()
    )
    return [
        {
            "weekday": i.weekday,
            "time_from": i.time_from.strftime("%H:%M") if i.time_from else None,
            "time_to": i.time_to.strftime("%H:%M") if i.time_to else None,
            "valid_from": i.valid_from.isoformat() if i.valid_from else None,
            "valid_to": i.valid_to.isoformat() if i.valid_to else None,
        }
        for i in items
    ]


@bp.route("/api/teachers/<int:teacher_id>/availability", methods=["GET"])
def get_teacher_availability(teacher_id):
    db = g.db
    teacher = db.query(Staff).filter(Staff.id == teacher_id, Staff.status == "active").first()
    if not teacher:
        return {"error": "Преподаватель не найден"}, 404

    start_str = request.args.get("start")
    days_str = request.args.get("days")
    duration_str = request.args.get("duration")
    step_str = request.args.get("step")

    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else date.today()
    except ValueError:
        return {"error": "start должен быть в формате YYYY-MM-DD"}, 400

    def _parse_positive_int(value, default, min_value, max_value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < min_value:
            return min_value
        if max_value is not None and parsed > max_value:
            return max_value
        return parsed

    days_count = _parse_positive_int(days_str, 7, 1, 21)
    duration_minutes = _parse_positive_int(duration_str, 60, 15, 240)
    step_minutes = _parse_positive_int(step_str, 30, 15, 180)

    working_hours = (
        db.query(TeacherWorkingHours)
        .filter_by(teacher_id=teacher_id, status="active")
        .all()
    )

    dates = []
    for offset in range(days_count):
        day = start_date + timedelta(days=offset)
        weekday = day.weekday()
        entries = [
            entry
            for entry in working_hours
            if entry.weekday == weekday
            and (not entry.valid_from or entry.valid_from <= day)
            and (not entry.valid_to or entry.valid_to >= day)
            and entry.time_from
            and entry.time_to
            and entry.time_to > entry.time_from
        ]
        busy_intervals = _collect_busy_intervals(db, teacher_id, day)
        service_start = _time_to_minutes(SERVICE_BREAK_START)
        service_end = _time_to_minutes(SERVICE_BREAK_END)
        if service_end > service_start:
            busy_intervals.append((service_start, service_end))
        busy_intervals.sort()
        slots = []
        seen = set()
        free_ranges = []
        for entry in entries:
            start_min = _time_to_minutes(entry.time_from)
            end_min = _time_to_minutes(entry.time_to)
            last_start = end_min - duration_minutes
            current = start_min
            while current <= last_start:
                if not _has_slot_conflict(current, duration_minutes, busy_intervals):
                    slot_str = _minutes_to_time_str(current)
                    if slot_str not in seen:
                        seen.add(slot_str)
                        slots.append(slot_str)
                current += step_minutes
            segments = _subtract_busy_intervals(start_min, end_min, busy_intervals)
            for seg_start, seg_end in segments:
                if seg_end - seg_start >= step_minutes:
                    free_ranges.append({
                        "from": _minutes_to_time_str(seg_start),
                        "to": _minutes_to_time_str(seg_end),
                        "from_minutes": seg_start,
                        "to_minutes": seg_end,
                    })
        dates.append(
            {
                "date": day.isoformat(),
                "weekday": weekday,
                "slots": slots,
                "free_ranges": free_ranges,
            }
        )

    return {
        "teacher_id": teacher_id,
        "teacher_name": teacher.name,
        "duration_minutes": duration_minutes,
        "slot_step_minutes": step_minutes,
        "dates": dates,
    }


@bp.route("/staff/list/all")
def list_all_staff():
    """
    Возвращает список всего персонала для администраторов
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter(Staff.status != "dismissed").all()
    
    result = []
    for s in staff:
        # Получаем username из User если есть telegram_id
        username = None
        if s.telegram_id:
            user = db.query(User).filter_by(telegram_id=s.telegram_id).first()
            if user:
                username = user.username
        can_edit, edit_block_reason = _staff_editability_payload(db, s)
        
        result.append({
            "id": s.id,
            "name": s.name,
            "position": s.position,
            "specialization": s.specialization,
            "phone": s.phone,
            "email": s.email,
            "telegram_id": s.telegram_id,
            "username": username,
            "photo": s.photo_path,
            "teaches": s.teaches,
            "status": s.status,
            "bio": s.bio,
            "can_edit": can_edit,
            "can_delete": can_edit,
            "edit_block_reason": edit_block_reason,
        })
    
    return jsonify(result)


@bp.route("/staff/search")
def search_staff():
    """
    Поиск пользователей для добавления в персонал.
    Параметры query:
    - q: строка поиска (если не указана, возвращает всех пользователей)
    - by_username: если True, ищет только по юзернейму (используется при @username)
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    try:
        db = g.db
        search_query = request.args.get('q', '').strip().lower()
        by_username = request.args.get('by_username', 'false').lower() == 'true'
        
        # щем среди пользователей (Users), а не среди персонала (Staff)
        users = db.query(User).all()
        result = []
        
        # Если нет поискового запроса, возвращаем всех пользователей
        if not search_query:
            result = [
                {
                    "id": u.id,
                    "name": u.name,
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "phone": u.phone,
                    "email": u.email
                }
                for u in users
            ]
        else:
            # Выполняем фильтр в зависимости от типа поиска
            for u in users:
                if by_username:
                    # Поиск только по юзернейму (при вводе @username)
                    if u.username:
                        # Нормализуем: убираем @ из обоих строк для сравнения
                        username_clean = u.username.lower().replace('@', '')
                        search_clean = search_query.replace('@', '')
                        if search_clean in username_clean or username_clean.startswith(search_clean):
                            result.append({
                                "id": u.id,
                                "name": u.name,
                                "telegram_id": u.telegram_id,
                                "username": u.username,
                                "phone": u.phone,
                                "email": u.email
                            })
                else:
                    # Поиск по имени или telegram_id (при обычном вводе)
                    if (u.name.lower().startswith(search_query) or 
                        (u.telegram_id and str(u.telegram_id).startswith(search_query))):
                        result.append({
                            "id": u.id,
                            "name": u.name,
                            "telegram_id": u.telegram_id,
                            "username": u.username,
                            "phone": u.phone,
                            "email": u.email
                        })
        
        return jsonify(result)
    except Exception:
        return internal_server_error_response(context="Failed to search users")


@bp.route("/search-users")
def search_users():
    """Поиск пользователей для рассылок"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        search_query = request.args.get('query', '').strip().lower()
        
        if not search_query:
            return jsonify([]), 200
        
        users = db.query(User).all()
        result = []
        
        for u in users:
            # Поиск по имени или telegram_id
            if (u.name.lower().find(search_query) != -1 or 
                (u.telegram_id and str(u.telegram_id).startswith(search_query))):
                result.append({
                    "id": u.id,
                    "name": u.name,
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "phone": u.phone,
                    "email": u.email
                })
        
        return jsonify(result)
    except Exception:
        return internal_server_error_response(context="Failed to search users")


@bp.route("/mailings", methods=["GET"])
def get_mailings():
    """Получает все рассылки (для управления)"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        mailings = db.query(Mailing).order_by(Mailing.created_at.desc()).all()
        
        result = []
        for m in mailings:
            result.append({
                "mailing_id": m.mailing_id,
                "creator_id": m.creator_id,
                "name": m.name,
                "description": m.description,
                "purpose": m.purpose,
                "status": m.status,
                "target_type": m.target_type,
                "target_id": m.target_id,
                "mailing_type": m.mailing_type,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
                "created_at": m.created_at.isoformat()
            })
        
        return jsonify(result)
    except Exception:
        return internal_server_error_response(context="Failed to list mailings")


@bp.route("/mailings", methods=["POST"])
def create_mailing():
    """Создает новую рассылку"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}
    
    try:
        # Обязательные поля
        if not data.get("creator_id") or not data.get("name") or not data.get("purpose") or not data.get("target_type"):
            return {"error": "creator_id, name, purpose и target_type обязательны"}, 400
        
        # Определяем статус на основе выбора пользователя
        send_now = data.get("send_now", False)
        
        # Если отправляем сейчас, статус = "pending" (ждет отправки)
        # Если отправляем позже, статус = "scheduled"
        status = "pending" if send_now else "scheduled"
        
        # Если нужно отправить сейчас
        sent_at = None
        if send_now:
            sent_at = None  # Отправляется в процессе, sent_at установится после отправки
        
        scheduled_at = data.get("scheduled_at")
        
        # Если это отложенная рассылка, нужно время
        if not send_now and not scheduled_at:
            return {"error": "Для отложенной рассылки требуется scheduled_at"}, 400
        
        # Если scheduled_at передана как строка, конвертируем в datetime
        if scheduled_at and isinstance(scheduled_at, str):
            # Убеждаемся что есть секунды в строке (datetime-local может их не содержать)
            if 'T' in scheduled_at and scheduled_at.count(':') == 1:
                scheduled_at = scheduled_at + ':00'  # Добавляем :00 для секунд
            try:
                scheduled_at = datetime.fromisoformat(scheduled_at)
            except ValueError as e:
                return {"error": f"Неверный формат даты: {e}"}, 400
        
        mailing = Mailing(
            creator_id=data["creator_id"],
            name=data["name"],
            description=data.get("description"),
            purpose=data["purpose"],
            status=status,
            target_type=data["target_type"],
            target_id=data.get("target_id"),
            mailing_type=data.get("mailing_type", "manual"),  # По умолчанию - ручная рассылка
            sent_at=sent_at,
            scheduled_at=scheduled_at
        )
        
        db.add(mailing)
        db.commit()
        
        # Если нужно отправить сейчас, добавляем в очередь отправки
        if send_now:
            from dance_studio.bot.bot import queue_mailing_for_sending
            queue_mailing_for_sending(mailing.mailing_id)
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }, 201
    
    except Exception:
        return internal_server_error_response(
            context="Failed to create mailing",
            db=db,
        )


@bp.route("/mailings/<int:mailing_id>", methods=["GET"])
def get_mailing(mailing_id):
    """Получает информацию о конкретной рассылке"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }
    
    except Exception:
        return internal_server_error_response(context="Failed to get mailing")


@bp.route("/mailings/<int:mailing_id>", methods=["PUT"])
def update_mailing(mailing_id):
    """Обновляет рассылку"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Обновляем поля
        if "name" in data:
            mailing.name = data["name"]
        if "description" in data:
            mailing.description = data["description"]
        if "purpose" in data:
            mailing.purpose = data["purpose"]
        if "status" in data:
            mailing.status = data["status"]
        if "target_type" in data:
            mailing.target_type = data["target_type"]
        if "target_id" in data:
            mailing.target_id = data["target_id"]
        if "mailing_type" in data:
            mailing.mailing_type = data["mailing_type"]
        if "sent_at" in data:
            mailing.sent_at = datetime.fromisoformat(data["sent_at"]) if data["sent_at"] else None
        if "scheduled_at" in data:
            mailing.scheduled_at = datetime.fromisoformat(data["scheduled_at"]) if data["scheduled_at"] else None
        
        db.commit()
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }
    
    except Exception:
        return internal_server_error_response(
            context="Failed to update mailing",
            db=db,
        )


@bp.route("/mailings/<int:mailing_id>", methods=["DELETE"])
def delete_mailing(mailing_id):
    """Удаляет рассылку (или отменяет её)"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Устанавливаем статус "отменено" вместо удаления
        mailing.status = "cancelled"
        db.commit()
        
        return {"message": "Рассылка отменена"}, 200
    
    except Exception:
        return internal_server_error_response(
            context="Failed to delete mailing",
            db=db,
        )


@bp.route("/mailings/<int:mailing_id>/send", methods=["POST"])
def send_mailing_endpoint(mailing_id):
    """нициирует отправку рассылки"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    try:
        # мпортируем функцию добавления рассылки в очередь
        from dance_studio.bot.bot import queue_mailing_for_sending
        
        db = g.db
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Проверяем, не отправлена ли уже
        if mailing.status == "sent":
            return {"error": "Рассылка уже была отправлена"}, 400
        
        if mailing.status == "cancelled":
            return {"error": "Рассылка была отменена"}, 400
        
        # Добавляем рассылку в очередь на отправку
        queue_mailing_for_sending(mailing_id)
        
        return {"message": f"Рассылка '{mailing.name}' добавлена в очередь отправки", "status": "pending"}, 200
    
    except Exception:
        return internal_server_error_response(context="Failed to enqueue mailing")


@bp.route("/api/directions", methods=["GET"])
def get_directions():
    """Получает все активные направления"""
    db = g.db
    direction_type = request.args.get("direction_type") or request.args.get("type")
    query = db.query(Direction).filter_by(status="active")
    if direction_type:
        direction_type = direction_type.lower()
        if direction_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
        query = query.filter(Direction.direction_type == direction_type)

    directions = query.order_by(Direction.created_at.desc()).all()

    #print(f"✓ Найдено {len(directions)} активных направлений")
    
    result = []
    for d in directions:
        groups_count = db.query(Group).filter_by(direction_id=d.direction_id).count()

        result.append(_serialize_direction_payload(d, groups_count=groups_count))

    return jsonify(result)


@bp.route("/api/directions/manage", methods=["GET"])
def get_directions_manage():
    """Получает все направления для управления (включая неактивные)"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    direction_type = request.args.get("direction_type") or request.args.get("type")
    query = db.query(Direction)
    if direction_type:
        direction_type = direction_type.lower()
        if direction_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
        query = query.filter(Direction.direction_type == direction_type)

    directions = query.order_by(Direction.created_at.desc()).all()
    
    result = []
    for d in directions:
        groups_count = db.query(Group).filter_by(direction_id=d.direction_id).count()

        result.append(
            _serialize_direction_payload(
                d,
                groups_count=groups_count,
                include_status=True,
                include_updated_at=True,
            )
        )

    return jsonify(result)


@bp.route("/api/directions/<int:direction_id>", methods=["GET"])
def get_direction(direction_id):
    """Возвращает одно направление по ID для формы редактирования"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    return jsonify(_serialize_direction_payload(direction, include_status=True, include_updated_at=True))


@bp.route("/api/directions/<int:direction_id>/groups", methods=["GET"])
def get_direction_groups(direction_id):
    """Возвращает список групп для направления"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    groups = db.query(Group).filter_by(direction_id=direction_id).order_by(Group.created_at.desc()).all()
    occupancy_map = get_group_occupancy_map(db, [group.id for group in groups if group and group.id])
    result = []
    for gr in groups:
        teacher_name = gr.teacher.name if gr.teacher else None
        teacher_photo = None
        if gr.teacher and gr.teacher.photo_path:
            teacher_photo = "/" + gr.teacher.photo_path.replace("\\", "/")
        occupied_students = int(occupancy_map.get(int(gr.id), 0))
        try:
            max_students = int(gr.max_students or 0)
        except (TypeError, ValueError):
            max_students = 0
        free_seats = max(0, max_students - occupied_students) if max_students > 0 else None
        result.append({
            "id": gr.id,
            "direction_id": gr.direction_id,
            "direction_type": direction.direction_type,
            "direction_title": _sanitize_direction_title(direction.title),
            "teacher_id": gr.teacher_id,
            "teacher_name": teacher_name,
            "teacher_photo": teacher_photo,
            "name": gr.name,
            "description": gr.description,
            "age_group": gr.age_group,
            "max_students": gr.max_students,
            "occupied_students": occupied_students,
            "free_seats": free_seats,
            "duration_minutes": gr.duration_minutes,
            "lessons_per_week": gr.lessons_per_week,
            "created_at": gr.created_at.isoformat()
        })

    return jsonify(result)


@bp.route("/api/directions/<int:direction_id>/groups", methods=["POST"])
def create_direction_group(direction_id):
    """Создает группу внутри направления"""
    perm_error = require_permission("create_group")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}

    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    name = data.get("name")
    teacher_id = data.get("teacher_id")
    age_group = data.get("age_group")
    max_students = data.get("max_students")
    duration_minutes = data.get("duration_minutes")
    lessons_per_week = data.get("lessons_per_week")
    description = data.get("description")

    if not name or not teacher_id or not age_group or not max_students or not duration_minutes:
        return {"error": "name, teacher_id, age_group, max_students, duration_minutes обязательны"}, 400

    teacher = db.query(Staff).filter_by(id=teacher_id).first()
    if not teacher:
        return {"error": "Преподаватель не найден"}, 404

    try:
        max_students_int = int(max_students)
        duration_minutes_int = int(duration_minutes)
    except ValueError:
        return {"error": "max_students и duration_minutes должны быть числами"}, 400

    lessons_per_week_int = None
    if lessons_per_week is not None and lessons_per_week != "":
        try:
            lessons_per_week_int = int(lessons_per_week)
        except ValueError:
            return {"error": "lessons_per_week должен быть числом"}, 400

    group = Group(
        direction_id=direction_id,
        teacher_id=teacher_id,
        name=name,
        description=description,
        age_group=age_group,
        max_students=max_students_int,
        duration_minutes=duration_minutes_int,
        lessons_per_week=lessons_per_week_int
    )
    db.add(group)
    db.commit()

    # Создаем чат Telegram через userbot и добавляем преподавателя
    if teacher.telegram_id:
        try:
            from dance_studio.bot.telegram_userbot import create_group_chat_sync

            teacher_user = db.query(User).filter_by(telegram_id=teacher.telegram_id).first()
            chat_info = create_group_chat_sync(
                name,
                [{
                    "id": teacher.telegram_id,
                    "username": getattr(teacher_user, "username", None),
                    "phone": teacher.phone,
                    "name": teacher.name,
                }],
            )
        except Exception as e:
            print(f"[create_direction_group] Telegram chat creation failed: {e}")
            chat_info = None
        if chat_info:
            group.chat_id = chat_info.get("chat_id")
            group.chat_invite_link = chat_info.get("invite_link")
            failed = chat_info.get("failed_user_ids") or []

            # Всегда шлём ссылку преподавателю, даже если invite сработал — на случай приватности.
            target_ids = {teacher.telegram_id} | {uid for uid in failed if uid}
            for uid in target_ids:
                try:
                    msg_text = f"Присоединиться к чату группы \"{name}\" можно по ссылке: {group.chat_invite_link}"
                    send_user_notification_sync(
                        user_id=int(uid),
                        text=msg_text,
                        context_note="Ссылка на чат группы"
                    )
                except Exception as send_err:
                    print(f"[create_direction_group] Не удалось отправить ссылку пользователю {uid}: {send_err}")
            db.commit()

    return {
        "id": group.id,
        "direction_id": group.direction_id,
        "teacher_id": group.teacher_id,
        "teacher_name": teacher.name,
        "name": group.name,
        "description": group.description,
        "age_group": group.age_group,
        "max_students": group.max_students,
        "duration_minutes": group.duration_minutes,
        "lessons_per_week": group.lessons_per_week,
        "chat_id": group.chat_id,
        "chat_invite_link": group.chat_invite_link,
        "created_at": group.created_at.isoformat()
    }, 201


@bp.route("/api/directions/create-session", methods=["POST"])
def create_direction_upload_session():
    """
    Создает сессию загрузки направления.
    Администратор заполняет форму и получает токен для бота.
    """
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}
    
    telegram_user_id = getattr(g, "telegram_id", None)
    if not telegram_user_id:
        return {"error": "Требуется авторизация"}, 401

    try:
        telegram_user_id = int(telegram_user_id)
    except (TypeError, ValueError):
        return {"error": "Неверный telegram_id"}, 400

    admin = db.query(Staff).filter_by(telegram_id=telegram_user_id).first()
    if not admin or admin.position not in ["администратор", "старший админ", "владелец", "тех. админ"]:
        return {"error": "У вас нет прав администратора"}, 403
    
    title = _sanitize_direction_title(data.get("title"))
    description = _sanitize_direction_description(data.get("description"))
    if not title:
        return {"error": "title обязателен"}, 400
    if not description:
        return {"error": "description обязателен"}, 400
    if not data.get("base_price"):
        return {"error": "base_price обязателен"}, 400

    direction_type = (data.get("direction_type") or "dance").lower()
    if direction_type not in ALLOWED_DIRECTION_TYPES:
        return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
    
    # Создаем сессию
    session_token = str(uuid.uuid4())
    
    session = DirectionUploadSession(
        admin_id=admin.id,
        telegram_user_id=telegram_user_id,
        title=title,
        direction_type=direction_type,
        description=description,
        base_price=data["base_price"],
        session_token=session_token,
        status="waiting_for_photo"
    )
    
    db.add(session)
    db.commit()
    
    return {
        "session_id": session.session_id,
        "session_token": session_token,
        "direction_type": direction_type,
        "message": "Сессия создана. Отправьте токен боту для загрузки фотографии."
    }, 201


@bp.route("/api/directions/upload-complete/<token>", methods=["GET"])
def get_upload_session_status(token):
    """Проверяет статус загрузки фотографии по токену"""
    try:
        db = g.db
        token_fp = token_fingerprint(token)

        session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
        if not session:
            current_app.logger.warning("direction upload status: session not found token_fp=%s", token_fp)
            return {"error": "Сессия не найдена"}, 404

        current_app.logger.info(
            "direction upload status token_fp=%s status=%s image=%s",
            token_fp,
            session.status,
            session.image_path,
        )

        return {
            "session_id": session.session_id,
            "status": session.status,
            "direction_type": session.direction_type or "dance",
            "image_path": _build_image_url(session.image_path),
            "title": _sanitize_direction_title(session.title),
            "description": _sanitize_direction_description(session.description),
            "base_price": session.base_price
        }
    except Exception:
        current_app.logger.exception("upload-complete error")
        return {"error": "internal"}, 500


@bp.route("/api/directions", methods=["POST"])
def create_direction():
    """Создает направление по сессии (с фото или без фото)"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}

    session_token = data.get("session_token")
    if not session_token:
        return {"error": "session_token обязателен"}, 400

    session_token_fp = token_fingerprint(session_token)
    current_app.logger.info(
        "create_direction request token_fp=%s direction_type=%s",
        session_token_fp,
        data.get("direction_type"),
    )

    session = db.query(DirectionUploadSession).filter_by(session_token=session_token).first()
    if not session:
        current_app.logger.warning("create_direction session not found token_fp=%s", session_token_fp)
        return {"error": "Сессия не найдена"}, 404

    current_app.logger.info(
        "create_direction session found token_fp=%s status=%s has_photo=%s",
        session_token_fp,
        session.status,
        bool(session.image_path),
    )

    allowed_statuses = {"waiting_for_photo", "photo_received"}
    if session.status not in allowed_statuses:
        return {"error": f"Сессия не готова. Статус: {session.status}"}, 400

    direction_type = (data.get("direction_type") or session.direction_type or "dance").lower()
    if direction_type not in ALLOWED_DIRECTION_TYPES:
        return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400

    direction = Direction(
        title=_sanitize_direction_title(session.title),
        direction_type=direction_type,
        description=_sanitize_direction_description(session.description),
        base_price=session.base_price,
        image_path=session.image_path if session.status == "photo_received" else None,
        is_popular=data.get("is_popular", 0),
        status="active"
    )

    db.add(direction)
    db.commit()

    session.status = "completed"
    db.commit()

    current_app.logger.info(
        "create_direction created id=%s type=%s token_fp=%s",
        direction.direction_id,
        direction.direction_type,
        session_token_fp,
    )

    return {
        "direction_id": direction.direction_id,
        "title": _sanitize_direction_title(direction.title),
        "direction_type": direction.direction_type,
        "message": "Направление успешно создано"
    }, 201


@bp.route("/api/directions/<int:direction_id>", methods=["PUT"])
def update_direction(direction_id):
    """Обновляет информацию о направлении"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404
    
    # Обновляем поля
    if "title" in data:
        title = _sanitize_direction_title(data["title"])
        if not title:
            return {"error": "title обязателен"}, 400
        direction.title = title
    if "description" in data:
        direction.description = _sanitize_direction_description(data["description"])
    if "base_price" in data:
        direction.base_price = data["base_price"]
    if "direction_type" in data:
        new_type = (data.get("direction_type") or "").lower()
        if new_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
        direction.direction_type = new_type
    if "status" in data:
        direction.status = data["status"]
    if "is_popular" in data:
        direction.is_popular = data["is_popular"]
    
    db.commit()
    
    return {
        "direction_id": direction.direction_id,
        "direction_type": direction.direction_type,
        "message": "Направление обновлено"
    }


@bp.route("/api/directions/<int:direction_id>", methods=["DELETE"])
def delete_direction(direction_id):
    """Удаляет направление"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404
    
    direction.status = "inactive"
    db.commit()
    
    return {"message": "Направление удалено"}


@bp.route("/api/directions/photo/<token>", methods=["POST"])
def upload_direction_photo(token):
    """
    API для загрузки фотографии направления
    спользуется ботом при получении фотографии от администратора
    """
    db = g.db
    token_fp = token_fingerprint(token)

    current_app.logger.info("direction photo upload start token_fp=%s", token_fp)

    session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
    if not session:
        current_app.logger.warning("direction upload: session not found token_fp=%s", token_fp)
        return {"error": "Сессия не найдена"}, 404

    if "photo" not in request.files:
        current_app.logger.warning("direction upload: no file provided token_fp=%s", token_fp)
        return {"error": "Файл не загружен"}, 400

    file = request.files["photo"]
    if file.filename == "":
        current_app.logger.warning("direction upload: empty filename token_fp=%s", token_fp)
        return {"error": "Файл не выбран"}, 400

    try:
        # Сохраняем в var/media/directions/<session_id>/photo_xxx.ext
        directions_dir = MEDIA_ROOT / "directions" / str(session.session_id)
        os.makedirs(directions_dir, exist_ok=True)

        # Сохраняем файл (расширение берем из mimetype/имени файла)
        mime = (getattr(file, "mimetype", "") or "").lower()
        orig_ext = os.path.splitext(file.filename or "")[1].lower()
        ext = orig_ext
        if mime in ("image/jpeg", "image/jpg"):
            ext = ".jpg"
        elif mime == "image/png":
            ext = ".png"
        elif mime == "image/webp":
            ext = ".webp"
        if not ext:
            return {"error": "Не удалось определить тип файла"}, 400
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in {".jpg", ".png", ".webp"}:
            return {"error": "Поддерживаются только JPG/PNG/WEBP"}, 400

        filename = secure_filename(f"photo_{session.session_id}{ext}")
        filepath = directions_dir / filename
        file.save(filepath)

        # Сохраняем путь в БД относительно корня проекта
        relative_path = os.path.relpath(filepath, PROJECT_ROOT)
        session.image_path = relative_path
        session.status = "photo_received"
        db.commit()

        current_app.logger.info(
            "direction upload success session_id=%s path=%s",
            session.session_id,
            filepath,
        )

        return {
            "message": "Фотография загружена",
            "session_id": session.session_id,
            "status": "photo_received",
            "image_path": _build_image_url(session.image_path),
        }, 200

    except Exception:
        return internal_server_error_response(
            context="Failed to upload direction photo",
            db=db,
        )


@bp.route("/api/system-settings/public", methods=["GET"])
def get_public_system_settings():
    db = g.db
    items = list_settings(db, public_only=True)
    db.commit()
    return jsonify({"items": items, "specs": list_setting_specs(public_only=True)})


@bp.route("/api/admin/system-settings", methods=["GET"])
def admin_get_system_settings():
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    db = g.db
    items = list_settings(db, public_only=False)
    db.commit()
    return jsonify({"items": items, "specs": list_setting_specs(public_only=False)})


@bp.route("/api/admin/system-settings/<path:key>", methods=["PUT"])
def admin_update_system_setting(key):
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    data = request.json or {}
    if "value" not in data:
        return {"error": "value is required"}, 400

    db = g.db
    staff = _get_current_staff(db)
    reason = data.get("reason")
    try:
        setting_payload = update_setting(
            db,
            key=key,
            raw_value=data.get("value"),
            changed_by_staff_id=(staff.id if staff else None),
            reason=reason,
            source="admin_api",
        )
    except KeyError as exc:
        return {"error": safe_client_error_message(exc)}, 404
    except SettingValidationError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    db.commit()
    return jsonify({"setting": setting_payload})


@bp.route("/api/admin/system-settings/changes", methods=["GET"])
def admin_get_system_settings_changes():
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    key = (request.args.get("key") or "").strip() or None
    limit_raw = request.args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 100
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}, 400

    db = g.db
    items = list_setting_changes(db, key=key, limit=limit)
    return jsonify({"items": items})


@bp.route("/api/admin/group-abonements/<int:abonement_id>/activate", methods=["POST"])
def admin_activate_abonement(abonement_id):
    """
    Активация абонемента админом после ручной проверки оплаты.
    Меняет статус абонемента с pending_payment на active и фиксирует подтвержденную оплату в PaymentTransaction.
    """
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    staff = _get_current_staff(db)
    try:
        apply_bundle = _parse_apply_bundle_flag(payload, default=True)
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "Абонемент не найден"}, 404
    target_rows = _collect_target_abonements(db, abonement, apply_bundle=apply_bundle)
    target_ids = [int(row.id) for row in target_rows if row and row.id]
    bundle_scope = len(target_ids) > 1

    blocked_rows = []
    pending_rows = []
    for row in target_rows:
        current_status = str(getattr(row, "status", "") or "").strip().lower()
        if current_status == ABONEMENT_STATUS_PENDING_PAYMENT:
            pending_rows.append(row)
            continue
        if current_status == ABONEMENT_STATUS_ACTIVE:
            continue
        blocked_rows.append({"id": int(row.id), "status": current_status or "unknown"})

    if blocked_rows:
        details = ", ".join(f"#{item['id']} ({item['status']})" for item in blocked_rows)
        return {
            "error": (
                "Нельзя активировать пакет: некоторые абонементы имеют неподдерживаемый статус: "
                f"{details}"
            )
        }, 400

    if not pending_rows:
        return {
            "status": ABONEMENT_STATUS_ACTIVE,
            "abonement_id": abonement.id,
            "payment_id": None,
            "activated_count": 0,
            "activated_abonement_ids": [],
            "affected_count": len(target_ids),
            "affected_abonement_ids": target_ids,
            "bundle_scope": bundle_scope,
            "bundle_id": str(getattr(abonement, "bundle_id", "") or "").strip() or None,
            "group_access_notified": True,
            "group_access_notify_error": "already_active",
        }

    payment = None
    payment_query = db.query(PaymentTransaction).filter(
        PaymentTransaction.user_id == abonement.user_id,
        PaymentTransaction.payment_type == "abonement",
    )
    if bundle_scope:
        payment_query = payment_query.filter(PaymentTransaction.object_id.in_(target_ids))
    else:
        payment_query = payment_query.filter(PaymentTransaction.object_id == abonement.id)
    payment = payment_query.order_by(PaymentTransaction.created_at.desc()).first()

    confirmed_at = datetime.utcnow()
    if payment:
        payment.status = "confirmed"
        payment.confirmed_at = confirmed_at
        if staff and not payment.confirmed_by_admin:
            payment.confirmed_by_admin = staff.id
    else:
        amount_raw = payload.get("amount")
        try:
            amount = int(amount_raw)
        except (TypeError, ValueError):
            return {"error": "Для активации без существующей оплаты передайте amount (> 0)"}, 400
        if amount <= 0:
            return {"error": "amount должен быть > 0"}, 400

        payment = PaymentTransaction(
            user_id=abonement.user_id,
            amount=amount,
            status="confirmed",
            payment_type="abonement",
            object_id=abonement.id,
            confirmed_by_admin=(staff.id if staff else None),
            confirmed_at=confirmed_at,
            comment=(str(payload.get("comment") or "").strip() or None),
        )
        db.add(payment)

    for target in pending_rows:
        try:
            set_abonement_status(target, ABONEMENT_STATUS_ACTIVE)
        except ValueError as exc:
            db.rollback()
            current_status = str(getattr(target, "status", "") or "").strip().lower()
            if current_status != ABONEMENT_STATUS_PENDING_PAYMENT:
                return {"error": f"Нельзя активировать абонемент из статуса '{current_status}'"}, 400
            return {"error": str(exc)}, 400

    db.commit()

    notify_target = target_rows[0] if bundle_scope else pending_rows[0]
    group_access_notified, group_access_notify_error = _notify_abonement_group_access_links(db, notify_target)
    activated_ids = [int(row.id) for row in pending_rows if row and row.id]

    return {
        "status": ABONEMENT_STATUS_ACTIVE,
        "abonement_id": abonement.id,
        "payment_id": payment.id if payment else None,
        "activated_count": len(activated_ids),
        "activated_abonement_ids": activated_ids,
        "affected_count": len(target_ids),
        "affected_abonement_ids": target_ids,
        "bundle_scope": bundle_scope,
        "bundle_id": str(getattr(abonement, "bundle_id", "") or "").strip() or None,
        "group_access_notified": group_access_notified,
        "group_access_notify_error": group_access_notify_error,
    }


@bp.route("/api/admin/groups/abonements", methods=["GET"])
def admin_list_groups_for_abonements():
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    rows = (
        db.query(Group, Direction, Staff)
        .join(Direction, Group.direction_id == Direction.direction_id)
        .join(Staff, Group.teacher_id == Staff.id)
        .order_by(Direction.title.asc(), Group.name.asc())
        .all()
    )

    items = []
    for group, direction, teacher in rows:
        items.append(
            {
                "id": group.id,
                "name": group.name,
                "direction_id": direction.direction_id if direction else None,
                "direction_title": direction.title if direction else None,
                "direction_type": direction.direction_type if direction else None,
                "teacher_id": teacher.id if teacher else None,
                "teacher_name": teacher.name if teacher else None,
                "lessons_per_week": group.lessons_per_week,
                "max_students": group.max_students,
            }
        )

    return jsonify({"items": items})


@bp.route("/api/admin/clients/merge", methods=["POST"])
def admin_merge_clients():
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    note = (payload.get("note") or "").strip()

    try:
        source_user_id = _parse_user_id_for_merge(payload, "source_user_id")
        target_user_id = _parse_user_id_for_merge(payload, "target_user_id")
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    if source_user_id == target_user_id:
        return {"error": "source_user_id and target_user_id must be different"}, 400

    source_user = db.query(User).filter_by(id=source_user_id).first()
    if not source_user:
        return {"error": "source user not found"}, 404

    target_user = db.query(User).filter_by(id=target_user_id).first()
    if not target_user:
        return {"error": "target user not found"}, 404

    if source_user.telegram_id:
        return {"error": "source user must not have telegram_id"}, 409
    if not target_user.telegram_id:
        return {"error": "target user must have telegram_id"}, 409

    if not target_user.username and source_user.username:
        target_user.username = source_user.username
    if not target_user.phone and source_user.phone:
        target_user.phone = source_user.phone
    if not target_user.email and source_user.email:
        target_user.email = source_user.email
    if not target_user.birth_date and source_user.birth_date:
        target_user.birth_date = source_user.birth_date
    if not target_user.photo_path and source_user.photo_path:
        target_user.photo_path = source_user.photo_path

    if source_user.user_notes:
        target_user.user_notes = _append_merge_note(target_user.user_notes, source_user.user_notes)
    if source_user.staff_notes:
        target_user.staff_notes = _append_merge_note(target_user.staff_notes, source_user.staff_notes)

    moved_group_abonements = int(
        db.query(GroupAbonement)
        .filter(GroupAbonement.user_id == source_user_id)
        .update({GroupAbonement.user_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_payments = int(
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.user_id == source_user_id)
        .update({PaymentTransaction.user_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_booking_requests = int(
        db.query(BookingRequest)
        .filter(BookingRequest.user_id == source_user_id)
        .update({BookingRequest.user_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_individual_lessons = int(
        db.query(IndividualLesson)
        .filter(IndividualLesson.student_id == source_user_id)
        .update({IndividualLesson.student_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_schedule_overrides = int(
        db.query(ScheduleOverrides)
        .filter(ScheduleOverrides.created_by_user_id == source_user_id)
        .update({ScheduleOverrides.created_by_user_id: target_user_id}, synchronize_session=False)
        or 0
    )

    attendance_result = _merge_attendance_rows(db, source_user_id, target_user_id)
    intentions_result = _merge_attendance_intentions_rows(db, source_user_id, target_user_id)
    reminders_result = _merge_attendance_reminders_rows(db, source_user_id, target_user_id)

    merged_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    merge_marker = f"Merged into user #{target_user_id} at {merged_at}"
    if note:
        merge_marker = f"{merge_marker}. Note: {note}"
    source_user.staff_notes = _append_merge_note(source_user.staff_notes, merge_marker)
    source_user.status = "inactive"
    source_user.telegram_id = None

    db.commit()

    return jsonify(
        {
            "ok": True,
            "source_user_id": source_user_id,
            "target_user_id": target_user_id,
            "moved": {
                "group_abonements": moved_group_abonements,
                "payment_transactions": moved_payments,
                "booking_requests": moved_booking_requests,
                "individual_lessons": moved_individual_lessons,
                "schedule_overrides": moved_schedule_overrides,
                "attendance": attendance_result,
                "attendance_intentions": intentions_result,
                "attendance_reminders": reminders_result,
            },
        }
    )


@bp.route("/api/admin/clients/<int:user_id>/sick-leave", methods=["POST"])
def admin_apply_client_sick_leave(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}

    try:
        date_from = _parse_iso_date(payload.get("date_from"), "date_from")
        date_to = _parse_iso_date(payload.get("date_to"), "date_to")
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    if date_to < date_from:
        return {"error": "date_to не может быть раньше date_from"}, 400

    note = (payload.get("note") or "").strip()
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404

    staff = _get_current_staff(db)
    now = datetime.utcnow()
    range_key = f"{date_from.isoformat()}:{date_to.isoformat()}"
    sick_default_comment = f"Болел: {date_from.isoformat()} - {date_to.isoformat()}"
    extension_days = (date_to - date_from).days + 1

    schedules = (
        db.query(Schedule)
        .filter(
            Schedule.object_type == "group",
            Schedule.date.isnot(None),
            Schedule.date >= date_from,
            Schedule.date <= date_to,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .order_by(Schedule.date.asc(), Schedule.time_from.asc())
        .all()
    )

    affected_schedule_ids = []
    created_attendance = 0
    updated_attendance = 0
    refunded_credits = 0
    affected_abonement_ids = set()

    for schedule in schedules:
        group_id = _schedule_group_id(schedule)
        if not group_id:
            continue

        abonement = _resolve_group_active_abonement(db, user.id, group_id, schedule.date)
        if not abonement:
            continue
        affected_abonement_ids.add(abonement.id)

        attendance = db.query(Attendance).filter_by(schedule_id=schedule.id, user_id=user.id).first()
        if not attendance:
            attendance = Attendance(
                schedule_id=schedule.id,
                user_id=user.id,
                status="sick",
                abonement_id=abonement.id,
                marked_at=now,
                marked_by_staff_id=staff.id if staff else None,
                comment=note or sick_default_comment,
            )
            db.add(attendance)
            db.flush()
            created_attendance += 1
        else:
            if attendance.status != "sick":
                updated_attendance += 1
            attendance.status = "sick"
            attendance.marked_at = now
            attendance.marked_by_staff_id = staff.id if staff else None
            if not attendance.abonement_id:
                attendance.abonement_id = abonement.id
            if note:
                attendance.comment = note
            elif not attendance.comment:
                attendance.comment = sick_default_comment

        affected_schedule_ids.append(schedule.id)

        debit_exists = db.query(GroupAbonementActionLog.id).filter_by(
            attendance_id=attendance.id,
            action_type="debit_attendance",
        ).first()
        refund_exists = db.query(GroupAbonementActionLog.id).filter_by(
            attendance_id=attendance.id,
            action_type="sick_leave_refund",
        ).first()
        if not debit_exists or refund_exists:
            continue

        refund_abonement_id = attendance.abonement_id or abonement.id
        refund_abonement = db.query(GroupAbonement).filter_by(id=refund_abonement_id).first()
        if not refund_abonement or refund_abonement.balance_credits is None:
            continue

        refund_abonement.balance_credits += 1
        refunded_credits += 1
        db.add(
            GroupAbonementActionLog(
                abonement_id=refund_abonement.id,
                action_type="sick_leave_refund",
                credits_delta=1,
                reason="sick_leave",
                note=f"Возврат занятия за больничный ({range_key})",
                attendance_id=attendance.id,
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "user_id": user.id,
                        "schedule_id": schedule.id,
                    },
                    ensure_ascii=False,
                ),
            )
        )

    extended_abonements = 0
    for abonement_id in affected_abonement_ids:
        abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
        if not abonement or not abonement.valid_to:
            continue

        duplicate_extension = db.query(GroupAbonementActionLog.id).filter_by(
            abonement_id=abonement.id,
            action_type="sick_leave_extend",
            reason=range_key,
        ).first()
        if duplicate_extension:
            continue

        abonement.valid_to = abonement.valid_to + timedelta(days=extension_days)
        extended_abonements += 1
        db.add(
            GroupAbonementActionLog(
                abonement_id=abonement.id,
                action_type="sick_leave_extend",
                credits_delta=0,
                reason=range_key,
                note=f"Продление абонемента на {extension_days} дн. (больничный)",
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "user_id": user.id,
                        "extension_days": extension_days,
                    },
                    ensure_ascii=False,
                ),
            )
        )

    db.commit()

    return {
        "ok": True,
        "user_id": user.id,
        "telegram_id": user.telegram_id,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "extension_days": extension_days,
        "affected_schedules": len(affected_schedule_ids),
        "created_attendance": created_attendance,
        "updated_attendance": updated_attendance,
        "refunded_credits": refunded_credits,
        "extended_abonements": extended_abonements,
    }, 200


@bp.route("/api/admin/clients/<int:user_id>/abonements", methods=["GET"])
def admin_get_client_abonements(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404

    raw_statuses = (request.args.get("statuses") or "").strip().lower()
    requested_statuses = []
    if raw_statuses:
        for token in raw_statuses.split(","):
            status = str(token or "").strip().lower()
            if not status:
                continue
            if status not in {
                ABONEMENT_STATUS_PENDING_PAYMENT,
                ABONEMENT_STATUS_ACTIVE,
                ABONEMENT_STATUS_EXPIRED,
                ABONEMENT_STATUS_CANCELLED,
            }:
                return {"error": f"Неизвестный статус абонемента: {status}"}, 400
            if status not in requested_statuses:
                requested_statuses.append(status)
    if not requested_statuses:
        requested_statuses = [ABONEMENT_STATUS_ACTIVE]

    items = (
        db.query(GroupAbonement)
        .filter(
            GroupAbonement.user_id == user.id,
            GroupAbonement.status.in_(requested_statuses),
        )
        .order_by(GroupAbonement.created_at.desc())
        .all()
    )
    return jsonify(
        {
            "user": {"id": user.id, "telegram_id": user.telegram_id, "name": user.name},
            "items": [_serialize_client_abonement_for_admin(db, item) for item in items],
        }
    )


@bp.route("/api/admin/clients/<int:user_id>/abonements", methods=["POST"])
def admin_create_client_abonement(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404

    payload = request.json or {}
    try:
        group_id = int(payload.get("group_id"))
    except (TypeError, ValueError):
        return {"error": "group_id должен быть целым числом"}, 400
    if group_id <= 0:
        return {"error": "group_id должен быть больше 0"}, 400

    abonement_type = str(payload.get("abonement_type") or "multi").strip().lower()
    if abonement_type not in {"single", "multi", "trial"}:
        return {"error": "abonement_type должен быть single, multi или trial"}, 400

    target_status = str(payload.get("status") or ABONEMENT_STATUS_ACTIVE).strip().lower()
    if target_status not in {ABONEMENT_STATUS_ACTIVE, ABONEMENT_STATUS_PENDING_PAYMENT}:
        return {"error": "status должен быть active или pending_payment"}, 400

    raw_bundle_group_ids = payload.get("bundle_group_ids")
    bundle_group_ids: list[int] = []
    if raw_bundle_group_ids not in (None, "", []):
        if not isinstance(raw_bundle_group_ids, list):
            return {"error": "bundle_group_ids должен быть массивом целых group_id"}, 400
        for raw_value in raw_bundle_group_ids:
            try:
                parsed_group_id = int(raw_value)
            except (TypeError, ValueError):
                return {"error": "bundle_group_ids должен быть массивом целых group_id"}, 400
            if parsed_group_id <= 0:
                return {"error": "bundle_group_ids должен содержать только положительные значения"}, 400
            if parsed_group_id not in bundle_group_ids:
                bundle_group_ids.append(parsed_group_id)

    if group_id not in bundle_group_ids:
        bundle_group_ids.insert(0, group_id)
    if not bundle_group_ids:
        bundle_group_ids = [group_id]
    if len(bundle_group_ids) > 3:
        return {"error": "Можно указать максимум 3 группы в мульти-абонементе"}, 400

    if abonement_type in {"single", "trial"} and len(bundle_group_ids) > 1:
        return {"error": "Для single/trial можно указать только одну группу"}, 400

    groups = db.query(Group).filter(Group.id.in_(bundle_group_ids)).all()
    groups_by_id = {int(row.id): row for row in groups if row and row.id}
    missing_group_ids = [gid for gid in bundle_group_ids if gid not in groups_by_id]
    if missing_group_ids:
        return {"error": f"Группы не найдены: {', '.join(str(v) for v in missing_group_ids)}"}, 404

    primary_group = groups_by_id[group_id]
    lessons_per_week = int(primary_group.lessons_per_week) if primary_group.lessons_per_week else 0

    if abonement_type == "multi":
        direction_ids = {row.direction_id for row in groups if row and row.direction_id}
        directions = db.query(Direction).filter(Direction.direction_id.in_(list(direction_ids))).all() if direction_ids else []
        direction_type_by_id = {
            int(direction.direction_id): str(direction.direction_type or "").strip().lower()
            for direction in directions
            if direction and direction.direction_id
        }
        base_direction_type = direction_type_by_id.get(int(primary_group.direction_id or 0), "")
        if not base_direction_type:
            return {"error": "Для основной группы не найдено направление"}, 400

        lessons_per_week_values = []
        for bundle_group_id in bundle_group_ids:
            bundle_group = groups_by_id[bundle_group_id]
            current_lessons_per_week = int(bundle_group.lessons_per_week or 0)
            if current_lessons_per_week <= 0:
                return {"error": "Для группы не настроено количество занятий в неделю"}, 400
            lessons_per_week_values.append(current_lessons_per_week)
            current_direction_type = direction_type_by_id.get(int(bundle_group.direction_id or 0), "")
            if current_direction_type != base_direction_type:
                return {"error": "Все группы в мульти-абонементе должны быть одного типа направления"}, 400
        unique_lessons_per_week = set(lessons_per_week_values)
        if len(unique_lessons_per_week) > 1:
            return {
                "error": (
                    "В комбо-абонементе все группы должны иметь одинаковое количество занятий в неделю "
                    "(и одинаковый месячный пакет: 4/8/12)."
                )
            }, 400
        lessons_per_week = lessons_per_week_values[0] if lessons_per_week_values else 0

    weeks_raw = payload.get("weeks")
    lessons_raw = payload.get("lessons")
    price_total_raw = payload.get("price_total_rub")
    weeks = None
    lessons = None

    if weeks_raw not in (None, ""):
        try:
            weeks = int(weeks_raw)
        except (TypeError, ValueError):
            return {"error": "weeks должен быть целым числом"}, 400
        if weeks <= 0:
            return {"error": "weeks должен быть больше 0"}, 400

    if lessons_raw not in (None, ""):
        try:
            lessons = int(lessons_raw)
        except (TypeError, ValueError):
            return {"error": "lessons должен быть целым числом"}, 400
        if lessons <= 0:
            return {"error": "lessons должен быть больше 0"}, 400

    if abonement_type == "multi":
        if lessons_per_week <= 0:
            return {"error": "Для группы не настроено количество занятий в неделю"}, 400

        if weeks is None and lessons is None:
            weeks = 4
            lessons = weeks * lessons_per_week
        elif weeks is None and lessons is not None:
            if lessons % lessons_per_week != 0:
                return {"error": f"lessons должен быть кратен {lessons_per_week}"}, 400
            weeks = lessons // lessons_per_week
        elif lessons is None and weeks is not None:
            lessons = weeks * lessons_per_week
        else:
            expected_lessons = weeks * lessons_per_week
            if lessons != expected_lessons:
                return {"error": f"Несоответствие: при {weeks} нед. должно быть {expected_lessons} занятий"}, 400
    else:
        weeks = weeks if weeks is not None else 1
        lessons = lessons if lessons is not None else 1

    valid_from = datetime.utcnow()
    valid_from_raw = (payload.get("valid_from") or "").strip()
    if valid_from_raw:
        try:
            valid_from = datetime.strptime(valid_from_raw, "%Y-%m-%d")
        except ValueError:
            return {"error": "valid_from должен быть в формате YYYY-MM-DD"}, 400
    valid_to = valid_from + timedelta(days=weeks * 7)

    staff = _get_current_staff(db)
    note = (payload.get("note") or "").strip()

    if price_total_raw in (None, ""):
        price_total_rub = 400
    else:
        try:
            price_total_rub = int(price_total_raw)
        except (TypeError, ValueError):
            return {"error": "price_total_rub Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð±Ñ‹Ñ‚ÑŒ Ñ†ÐµÐ»Ñ‹Ð¼ Ñ‡Ð¸ÑÐ»Ð¾Ð¼"}, 400
        if price_total_rub < 0:
            return {"error": "price_total_rub Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð±Ñ‹Ñ‚ÑŒ Ð±Ð¾Ð»ÑŒÑˆÐµ Ð¸Ð»Ð¸ Ñ€Ð°Ð²ÐµÐ½ 0"}, 400

    if abonement_type in {"single", "trial"}:
        bundle_group_ids = [group_id]

    bundle_size = len(bundle_group_ids) if abonement_type == "multi" else 1
    bundle_id = str(uuid.uuid4()) if bundle_size > 1 else None
    total_lessons = int(lessons or 0) * max(1, int(bundle_size or 1))
    price_per_lesson = None
    if total_lessons > 0:
        price_per_lesson = max(0, int(price_total_rub) // int(total_lessons))
    created_abonements: list[GroupAbonement] = []
    for bundle_group_id in bundle_group_ids:
        abonement = GroupAbonement(
            user_id=user.id,
            group_id=bundle_group_id,
            abonement_type=abonement_type,
            bundle_id=bundle_id,
            bundle_size=bundle_size,
            balance_credits=lessons,
            price_total_rub=price_total_rub,
            lessons_total=total_lessons if total_lessons > 0 else None,
            price_per_lesson_rub=price_per_lesson,
            status=target_status,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        db.add(abonement)
        created_abonements.append(abonement)

    db.flush()

    for abonement in created_abonements:
        db.add(
            GroupAbonementActionLog(
                abonement_id=abonement.id,
                action_type="manual_issue_abonement",
                credits_delta=lessons,
                reason=(
                    f"status={target_status};weeks={weeks};lessons={lessons};"
                    f"bundle_size={bundle_size}"
                ),
                note=note or "Ручная выдача абонемента администратором",
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "user_id": user.id,
                        "group_id": abonement.group_id,
                        "bundle_group_ids": bundle_group_ids,
                        "abonement_type": abonement_type,
                        "status": target_status,
                        "weeks": weeks,
                        "lessons": lessons,
                        "lessons_per_week": lessons_per_week,
                        "bundle_size": bundle_size,
                        "price_total_rub": price_total_rub,
                        "lessons_total": total_lessons if total_lessons > 0 else None,
                        "price_per_lesson_rub": price_per_lesson,
                    },
                    ensure_ascii=False,
                ),
            )
        )

    db.commit()

    primary_abonement = created_abonements[0]
    group_access_notified = False
    group_access_notify_error = None
    if target_status == ABONEMENT_STATUS_ACTIVE:
        group_access_notified, group_access_notify_error = _notify_abonement_group_access_links(db, primary_abonement)

    return (
        jsonify(
            {
                "ok": True,
                "abonement": _serialize_client_abonement_for_admin(db, primary_abonement),
                "issued_abonements": [_serialize_client_abonement_for_admin(db, row) for row in created_abonements],
                "bundle_group_ids": bundle_group_ids,
                "bundle_size": bundle_size,
                "group_access_notified": group_access_notified,
                "group_access_notify_error": group_access_notify_error,
            }
        ),
        201,
    )


@bp.route("/api/admin/clients/<int:user_id>/telegram-photo", methods=["GET"])
def admin_get_client_telegram_photo(user_id: int):
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404
    if not user.telegram_id:
        return {"error": "У клиента нет telegram_id"}, 404
    if not BOT_TOKEN:
        return {"error": "Telegram Bot API не настроен"}, 503

    try:
        profile_resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
            params={"user_id": user.telegram_id, "limit": 1},
            timeout=5,
        )
        profile_resp.raise_for_status()
        profile_data = profile_resp.json()
        photos = profile_data.get("result", {}).get("photos") or []
        if not profile_data.get("ok") or not photos or not photos[0]:
            return {"error": "Фото профиля Telegram не найдено"}, 404

        file_id = (photos[0][-1] or {}).get("file_id")
        if not file_id:
            return {"error": "Фото профиля Telegram не найдено"}, 404

        file_resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=5,
        )
        file_resp.raise_for_status()
        file_data = file_resp.json()
        file_path = ((file_data.get("result") or {}).get("file_path") or "").strip()
        if not file_data.get("ok") or not file_path:
            return {"error": "Не удалось получить файл фото из Telegram"}, 404

        image_resp = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
            timeout=10,
        )
        image_resp.raise_for_status()
        if not image_resp.content:
            return {"error": "Пустой ответ от Telegram"}, 404
    except requests.RequestException:
        current_app.logger.exception(
            "Failed to proxy client telegram photo user_id=%s telegram_id=%s",
            user_id,
            user.telegram_id,
        )
        return {"error": "Не удалось получить фото из Telegram"}, 502
    except ValueError:
        current_app.logger.exception(
            "Failed to decode telegram photo response user_id=%s telegram_id=%s",
            user_id,
            user.telegram_id,
        )
        return {"error": "Некорректный ответ Telegram"}, 502

    response = current_app.response_class(
        image_resp.content,
        mimetype=(image_resp.headers.get("Content-Type") or "image/jpeg"),
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.route("/api/admin/clients/<int:user_id>/attendance-calendar", methods=["GET"])
def admin_get_client_attendance_calendar(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404

    month_param = request.args.get("month")
    try:
        month_start = _parse_month_start(month_param)
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1)

    schedules = (
        db.query(Schedule)
        .filter(
            Schedule.object_type == "group",
            Schedule.date.isnot(None),
            Schedule.date >= month_start,
            Schedule.date < month_end,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .order_by(Schedule.date.asc(), Schedule.time_from.asc())
        .all()
    )

    schedule_ids = [s.id for s in schedules]
    attendance_by_schedule_id = {}
    if schedule_ids:
        for row in db.query(Attendance).filter(
            Attendance.user_id == user.id,
            Attendance.schedule_id.in_(schedule_ids),
        ).all():
            attendance_by_schedule_id[row.schedule_id] = row

    group_ids = sorted({
        _schedule_group_id(s) for s in schedules
        if _schedule_group_id(s)
    })
    groups = {}
    directions = {}
    if group_ids:
        for g_row in db.query(Group).filter(Group.id.in_(group_ids)).all():
            groups[g_row.id] = g_row
            if g_row.direction_id:
                directions[g_row.direction_id] = None
        direction_ids = [d_id for d_id in directions.keys()]
        if direction_ids:
            for d_row in db.query(Direction).filter(Direction.direction_id.in_(direction_ids)).all():
                directions[d_row.direction_id] = d_row

    entries = []
    for schedule in schedules:
        group_id = _schedule_group_id(schedule)
        if not group_id:
            continue

        attendance = attendance_by_schedule_id.get(schedule.id)
        enrolled = bool(_resolve_group_active_abonement(db, user.id, group_id, schedule.date))
        if not enrolled and not attendance:
            continue

        mark_code = None
        mark_label = None
        status = attendance.status if attendance else "planned"
        if status in {"present", "late"}:
            mark_code = "П"
            mark_label = "Пришел"
        elif status == "absent":
            mark_code = "Н"
            mark_label = "Неявка"
        elif status == "sick":
            mark_code = "Б"
            mark_label = "Больничный"
        elif status == "planned":
            mark_code = None
            mark_label = "Записан"

        group = groups.get(group_id)
        direction = directions.get(group.direction_id) if group and group.direction_id else None
        entries.append(
            {
                "date": schedule.date.isoformat(),
                "schedule_id": schedule.id,
                "group_id": group_id,
                "group_name": group.name if group else None,
                "direction_title": direction.title if direction else None,
                "time_from": schedule.time_from.strftime("%H:%M") if schedule.time_from else None,
                "time_to": schedule.time_to.strftime("%H:%M") if schedule.time_to else None,
                "status": status,
                "mark_code": mark_code,
                "mark_label": mark_label,
            }
        )

    return jsonify(
        {
            "user": {"id": user.id, "telegram_id": user.telegram_id, "name": user.name},
            "month": month_start.strftime("%Y-%m"),
            "entries": entries,
            "legend": {
                "П": "Пришел",
                "Н": "Неявка",
                "Б": "Больничный",
            },
        }
    )


@bp.route("/api/admin/group-abonements/<int:abonement_id>/extend", methods=["POST"])
def admin_extend_group_abonement(abonement_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    try:
        apply_bundle = _parse_apply_bundle_flag(payload, default=True)
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400
    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "Абонемент не найден"}, 404
    target_rows = _collect_target_abonements(db, abonement, apply_bundle=apply_bundle)
    target_ids = [int(row.id) for row in target_rows if row and row.id]
    bundle_scope = len(target_ids) > 1

    lessons_per_week_values = []
    for target in target_rows:
        target_status = str(getattr(target, "status", "") or "").strip().lower()
        if target_status == ABONEMENT_STATUS_CANCELLED:
            return {
                "error": (
                    "Нельзя продлить пакет: один из абонементов уже отменен "
                    f"(#{int(target.id)})"
                )
            }, 400
        target_group = db.query(Group).filter_by(id=target.group_id).first()
        target_lessons_per_week = (
            int(target_group.lessons_per_week) if target_group and target_group.lessons_per_week else None
        )
        if not target_lessons_per_week or target_lessons_per_week <= 0:
            return {"error": f"Для группы в абонементе #{int(target.id)} не настроено количество занятий в неделю"}, 400
        lessons_per_week_values.append(int(target_lessons_per_week))
    lessons_per_week = min(lessons_per_week_values) if lessons_per_week_values else None

    weeks_raw = payload.get("weeks")
    lessons_raw = payload.get("lessons")
    if weeks_raw in (None, "") and lessons_raw in (None, ""):
        return {"error": "Укажите weeks или lessons"}, 400

    weeks = None
    lessons = None
    if weeks_raw not in (None, ""):
        try:
            weeks = int(weeks_raw)
        except (TypeError, ValueError):
            return {"error": "weeks должен быть целым числом"}, 400
        if weeks <= 0:
            return {"error": "weeks должен быть больше 0"}, 400
    if lessons_raw not in (None, ""):
        try:
            lessons = int(lessons_raw)
        except (TypeError, ValueError):
            return {"error": "lessons должен быть целым числом"}, 400
        if lessons <= 0:
            return {"error": "lessons должен быть больше 0"}, 400

    if weeks is None and lessons is not None:
        if lessons % lessons_per_week != 0:
            return {"error": f"lessons должен быть кратен {lessons_per_week}"}, 400
        weeks = lessons // lessons_per_week
    elif lessons is None and weeks is not None:
        lessons = weeks * lessons_per_week
    else:
        expected_lessons = weeks * lessons_per_week
        if lessons != expected_lessons:
            return {"error": f"Несоответствие: при {weeks} нед. должно быть {expected_lessons} занятий"}, 400

    note = (payload.get("note") or "").strip()
    staff = _get_current_staff(db)
    now = datetime.utcnow()
    for target in target_rows:
        target.balance_credits = int(target.balance_credits or 0) + lessons
        valid_to_base = target.valid_to if (target.valid_to and target.valid_to > now) else now
        target.valid_to = valid_to_base + timedelta(days=weeks * 7)
        db.add(
            GroupAbonementActionLog(
                abonement_id=target.id,
                action_type="manual_extend_abonement",
                credits_delta=lessons,
                reason=f"weeks={weeks};lessons={lessons};scope={'bundle' if bundle_scope else 'single'}",
                note=note or f"Продление абонемента: +{weeks} нед. / +{lessons} занятий",
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "weeks": weeks,
                        "lessons": lessons,
                        "lessons_per_week": lessons_per_week,
                        "user_id": target.user_id,
                        "group_id": target.group_id,
                        "apply_bundle": bundle_scope,
                        "bundle_id": str(getattr(target, "bundle_id", "") or "").strip() or None,
                        "affected_abonement_ids": target_ids,
                    },
                    ensure_ascii=False,
                ),
            )
        )
    db.commit()

    return jsonify(
        {
            "ok": True,
            "abonement": _serialize_client_abonement_for_admin(db, abonement),
            "affected_count": len(target_ids),
            "affected_abonement_ids": target_ids,
            "bundle_scope": bundle_scope,
            "bundle_id": str(getattr(abonement, "bundle_id", "") or "").strip() or None,
            "applied": {
                "weeks": weeks,
                "lessons": lessons,
                "lessons_per_week": lessons_per_week,
            },
        }
    )


@bp.route("/api/admin/group-abonements/<int:abonement_id>/cancel", methods=["POST"])
def admin_cancel_group_abonement(abonement_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    try:
        apply_bundle = _parse_apply_bundle_flag(payload, default=True)
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400
    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "Абонемент не найден"}, 404
    target_rows = _collect_target_abonements(db, abonement, apply_bundle=apply_bundle)
    target_ids = [int(row.id) for row in target_rows if row and row.id]
    bundle_scope = len(target_ids) > 1

    all_cancelled = True
    expired_rows = []
    for row in target_rows:
        current_status = str(getattr(row, "status", "") or "").strip().lower()
        if current_status != ABONEMENT_STATUS_CANCELLED:
            all_cancelled = False
        if current_status == ABONEMENT_STATUS_EXPIRED:
            expired_rows.append(int(row.id))

    if all_cancelled:
        return {"error": "Абонемент уже отменен"}, 409
    if expired_rows:
        if len(expired_rows) == 1:
            return {"error": f"Нельзя отменить уже истекший абонемент #{expired_rows[0]}"}, 400
        return {"error": "Нельзя отменить пакет: часть абонементов уже истекла"}, 400

    note = (payload.get("note") or "").strip()
    staff = _get_current_staff(db)
    affected_ids = []
    for target in target_rows:
        current_status = str(getattr(target, "status", "") or "").strip().lower()
        if current_status == ABONEMENT_STATUS_CANCELLED:
            continue
        if current_status == ABONEMENT_STATUS_PENDING_PAYMENT:
            target.status = ABONEMENT_STATUS_CANCELLED
        else:
            try:
                set_abonement_status(target, ABONEMENT_STATUS_CANCELLED)
            except ValueError as exc:
                return {"error": safe_client_error_message(exc)}, 400

        db.add(
            GroupAbonementActionLog(
                abonement_id=target.id,
                action_type="manual_cancel_abonement",
                credits_delta=0,
                reason=f"status_from={current_status};scope={'bundle' if bundle_scope else 'single'}",
                note=note or "Ручная отмена абонемента администратором",
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "status_from": current_status,
                        "status_to": ABONEMENT_STATUS_CANCELLED,
                        "user_id": target.user_id,
                        "group_id": target.group_id,
                        "apply_bundle": bundle_scope,
                        "bundle_id": str(getattr(target, "bundle_id", "") or "").strip() or None,
                        "affected_abonement_ids": target_ids,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        affected_ids.append(int(target.id))
    db.commit()

    return jsonify(
        {
            "ok": True,
            "abonement": _serialize_client_abonement_for_admin(db, abonement),
            "affected_count": len(affected_ids),
            "affected_abonement_ids": affected_ids,
            "bundle_scope": bundle_scope,
            "bundle_id": str(getattr(abonement, "bundle_id", "") or "").strip() or None,
        }
    )


@bp.route("/api/admin/group-abonements/<int:abonement_id>/adjust-credits", methods=["POST"])
def admin_adjust_group_abonement_credits(abonement_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    try:
        delta_credits = int(payload.get("delta_credits"))
    except (TypeError, ValueError):
        return {"error": "delta_credits должен быть целым числом"}, 400
    if delta_credits == 0:
        return {"error": "delta_credits не должен быть равен 0"}, 400
    try:
        apply_bundle = _parse_apply_bundle_flag(payload, default=True)
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "Абонемент не найден"}, 404
    target_rows = _collect_target_abonements(db, abonement, apply_bundle=apply_bundle)
    target_ids = [int(row.id) for row in target_rows if row and row.id]
    bundle_scope = len(target_ids) > 1

    cancelled_rows = []
    insufficient_rows = []
    for target in target_rows:
        current_status = str(getattr(target, "status", "") or "").strip().lower()
        if current_status == ABONEMENT_STATUS_CANCELLED:
            cancelled_rows.append(int(target.id))
            continue
        before_balance = int(getattr(target, "balance_credits", 0) or 0)
        if delta_credits < 0 and before_balance + delta_credits < 0:
            insufficient_rows.append({"id": int(target.id), "balance": before_balance})

    if cancelled_rows:
        if len(cancelled_rows) == 1:
            return {"error": f"Нельзя корректировать отмененный абонемент #{cancelled_rows[0]}"}, 400
        return {"error": "Нельзя корректировать пакет: часть абонементов уже отменена"}, 400
    if insufficient_rows:
        details = ", ".join(
            f"#{item['id']} (остаток {item['balance']})" for item in insufficient_rows
        )
        return {"error": f"Недостаточно занятий для списания: {details}"}, 400

    note = (payload.get("note") or "").strip()
    staff = _get_current_staff(db)
    affected = []
    for target in target_rows:
        before_balance = int(getattr(target, "balance_credits", 0) or 0)
        after_balance = before_balance + delta_credits
        target.balance_credits = after_balance
        db.add(
            GroupAbonementActionLog(
                abonement_id=target.id,
                action_type="manual_adjust_credits",
                credits_delta=delta_credits,
                reason=f"delta={delta_credits};scope={'bundle' if bundle_scope else 'single'}",
                note=note or "Ручная корректировка баланса занятий администратором",
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "delta_credits": delta_credits,
                        "before_balance": before_balance,
                        "after_balance": after_balance,
                        "user_id": target.user_id,
                        "group_id": target.group_id,
                        "apply_bundle": bundle_scope,
                        "bundle_id": str(getattr(target, "bundle_id", "") or "").strip() or None,
                        "affected_abonement_ids": target_ids,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        affected.append({"id": int(target.id), "balance_credits": after_balance})

    db.commit()
    return jsonify(
        {
            "ok": True,
            "abonement": _serialize_client_abonement_for_admin(db, abonement),
            "delta_credits": delta_credits,
            "affected_count": len(affected),
            "affected_abonement_ids": [item["id"] for item in affected],
            "affected": affected,
            "bundle_scope": bundle_scope,
            "bundle_id": str(getattr(abonement, "bundle_id", "") or "").strip() or None,
        }
    )


# -------------------- DISCOUNTS --------------------

def _serialize_user_discount(discount: UserDiscount) -> dict:
    return {
        "id": discount.id,
        "discount_type": discount.discount_type,
        "value": discount.value,
        "is_one_time": bool(discount.is_one_time),
        "is_active": bool(discount.is_active),
        "usage_state": resolve_discount_usage_state(discount),
        "consumed_at": discount.consumed_at.isoformat() if discount.consumed_at else None,
        "consumed_booking_id": discount.consumed_booking_id,
        "comment": discount.comment,
        "created_at": discount.created_at.isoformat() if discount.created_at else None,
    }


def _validate_discount_payload(payload: dict) -> tuple[str, int, bool, str | None]:
    discount_type = str(payload.get("discount_type") or "").strip().lower()
    if discount_type not in {"percentage", "fixed"}:
        raise ValueError("discount_type должен быть percentage или fixed")

    raw_value = payload.get("value")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("value должен быть целым числом")

    if discount_type == "percentage":
        if value < 1 or value > 100:
            raise ValueError("percentage value должен быть в диапазоне 1..100")
    elif value < 1:
        raise ValueError("fixed value должен быть >= 1")

    is_one_time = bool(payload.get("is_one_time", True))
    comment_raw = payload.get("comment")
    comment = str(comment_raw).strip() if comment_raw is not None else None
    return discount_type, value, is_one_time, (comment or None)


@bp.route("/api/admin/users/<int:user_id>/discounts", methods=["GET"])
def admin_get_user_discounts(user_id):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    discounts = db.query(UserDiscount).filter_by(user_id=user_id).order_by(UserDiscount.created_at.desc()).all()
    return jsonify([_serialize_user_discount(d) for d in discounts])


@bp.route("/api/admin/users/<int:user_id>/discounts", methods=["POST"])
def admin_add_user_discount(user_id):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}

    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Пользователь не найден"}, 404

    try:
        discount_type, value, is_one_time, comment = _validate_discount_payload(data)
    except ValueError as exc:
        return {"error": safe_client_error_message(exc)}, 400

    discount = UserDiscount(
        user_id=user_id,
        discount_type=discount_type,
        value=value,
        is_one_time=is_one_time,
        is_active=True,
        comment=comment,
    )
    db.add(discount)
    db.commit()

    return {"ok": True, "discount": _serialize_user_discount(discount)}, 201


@bp.route("/api/admin/discounts/<int:discount_id>", methods=["DELETE"])
def admin_deactivate_discount(discount_id):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    discount = db.query(UserDiscount).filter_by(id=discount_id).first()
    if discount and discount.is_active:
        discount.is_active = False
    db.commit()
    return {"ok": True}

