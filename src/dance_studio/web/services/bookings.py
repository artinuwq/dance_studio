from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Iterable

import requests
from flask import current_app
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

from dance_studio.core.abonement_pricing import (
    ABONEMENT_TYPE_MULTI,
    ABONEMENT_TYPE_TRIAL,
    AbonementPricingError,
    get_next_group_date as pricing_get_next_group_date,
    parse_booking_bundle_group_ids,
    quote_group_booking,
)
from dance_studio.core.booking_utils import build_booking_keyboard_data, format_booking_message
from dance_studio.core.statuses import (
    ABONEMENT_STATUS_CANCELLED,
    ABONEMENT_STATUS_EXPIRED,
    BOOKING_ACTIVE_STATUSES,
    BOOKING_PAYMENT_CONFIRMED_STATUSES,
    BOOKING_STATUS_CREATED,
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_WAITING_PAYMENT,
    normalize_abonement_status,
    normalize_booking_status,
    set_booking_status,
)
from dance_studio.core.notification_service import send_user_notification_sync
from dance_studio.core.config import PROJECT_NAME_FULL
from dance_studio.core.system_settings_service import get_setting_value
from dance_studio.db.models import BookingRequest, Group, GroupAbonement, HallRental, IndividualLesson, Schedule, User
from dance_studio.web.constants import INACTIVE_SCHEDULE_STATUSES
from dance_studio.web.services.payments import _resolve_payment_profile_payload_for_booking


BOOKING_SEAT_OCCUPYING_STATUSES = set(BOOKING_ACTIVE_STATUSES)
BOOKING_RESERVATION_EXPIRABLE_STATUSES = {BOOKING_STATUS_WAITING_PAYMENT}
DEFAULT_GROUP_BOOKING_RESERVE_MINUTES = 48 * 60
_TERMINAL_ABONEMENT_STATUSES = {
    ABONEMENT_STATUS_CANCELLED,
    ABONEMENT_STATUS_EXPIRED,
}

_BOOKING_DUPLICATE_INDEX_NAMES = {
    "uq_booking_req_user_slot_active",
    "uq_booking_req_user_group_active",
}


class BookingConstraintError(ValueError):
    """Base class for booking guard errors."""


class BookingAlreadyExistsError(BookingConstraintError):
    """Raised when a user is already booked into the same slot."""


class BookingCapacityExceededError(BookingConstraintError):
    """Raised when group capacity has no available seats."""


class BookingReservationExpiredError(BookingConstraintError):
    """Raised when payment is attempted after reservation deadline."""


def _is_duplicate_booking_integrity_error(exc: IntegrityError) -> bool:
    raw = str(getattr(exc, "orig", exc) or "").lower()
    if "unique constraint failed" in raw and "booking_requests" in raw:
        return True
    if "duplicate key value violates unique constraint" in raw and "booking_req" in raw:
        return True
    return any(index_name in raw for index_name in _BOOKING_DUPLICATE_INDEX_NAMES)


def _reservation_deadline(now: datetime, reserve_minutes: int | None) -> datetime:
    ttl_minutes = int(reserve_minutes or DEFAULT_GROUP_BOOKING_RESERVE_MINUTES)
    if ttl_minutes <= 0:
        ttl_minutes = DEFAULT_GROUP_BOOKING_RESERVE_MINUTES
    return now + timedelta(minutes=ttl_minutes)


def is_booking_reservation_expired(booking: BookingRequest, *, now: datetime | None = None) -> bool:
    if normalize_booking_status(getattr(booking, "status", None), default="") != BOOKING_STATUS_WAITING_PAYMENT:
        return False
    if not getattr(booking, "reserved_until", None):
        return False
    current_time = now or datetime.utcnow()
    return bool(booking.reserved_until <= current_time)


def expire_stale_booking_reservations(
    db,
    *,
    now: datetime | None = None,
    group_id: int | None = None,
    booking_id: int | None = None,
) -> set[int]:
    current_time = now or datetime.utcnow()
    query = db.query(BookingRequest).filter(
        BookingRequest.status.in_(tuple(BOOKING_RESERVATION_EXPIRABLE_STATUSES)),
        BookingRequest.reserved_until.isnot(None),
        BookingRequest.reserved_until <= current_time,
    )
    if group_id is not None:
        query = query.filter(BookingRequest.group_id == int(group_id))
    if booking_id is not None:
        query = query.filter(BookingRequest.id == int(booking_id))

    expired_rows = query.all()
    expired_ids: set[int] = set()
    for row in expired_rows:
        if normalize_booking_status(row.status, default="") != BOOKING_STATUS_WAITING_PAYMENT:
            continue
        set_booking_status(
            row,
            BOOKING_STATUS_CANCELLED,
            actor_name="system: reservation timeout",
            changed_at=current_time,
            allow_same=False,
        )
        row.reserved_until = None
        expired_ids.add(int(row.id))
    return expired_ids


def _has_duplicate_non_group_booking(db, booking: BookingRequest) -> bool:
    if not booking.date or not booking.time_from or not booking.time_to:
        return False
    duplicate = (
        db.query(BookingRequest.id)
        .filter(
            BookingRequest.user_id == int(booking.user_id),
            BookingRequest.object_type == str(booking.object_type),
            BookingRequest.date == booking.date,
            BookingRequest.time_from == booking.time_from,
            BookingRequest.time_to == booking.time_to,
            BookingRequest.status.in_(tuple(BOOKING_SEAT_OCCUPYING_STATUSES)),
        )
        .first()
    )
    return duplicate is not None


def _matching_group_abonements_for_booking(db, booking: BookingRequest) -> list[GroupAbonement]:
    if getattr(booking, "object_type", None) != "group":
        return []
    if not getattr(booking, "user_id", None) or not getattr(booking, "group_id", None):
        return []

    group_ids = parse_booking_bundle_group_ids(booking)
    if not group_ids:
        group_ids = [int(booking.group_id)]

    query = db.query(GroupAbonement).filter(
        GroupAbonement.user_id == int(booking.user_id),
        GroupAbonement.group_id.in_(group_ids),
    )
    if booking.abonement_type:
        query = query.filter(GroupAbonement.abonement_type == str(booking.abonement_type).strip().lower())
    if booking.group_start_date:
        window_start = datetime.combine(booking.group_start_date, time.min)
        window_end = datetime.combine(booking.group_start_date, time.max)
        query = query.filter(
            GroupAbonement.valid_from.isnot(None),
            GroupAbonement.valid_from >= window_start,
            GroupAbonement.valid_from <= window_end,
        )
    if booking.valid_until:
        window_start = datetime.combine(booking.valid_until, time.min)
        window_end = datetime.combine(booking.valid_until, time.max)
        query = query.filter(
            GroupAbonement.valid_to.isnot(None),
            GroupAbonement.valid_to >= window_start,
            GroupAbonement.valid_to <= window_end,
        )
    return query.all()


def _is_stale_group_booking(db, booking: BookingRequest) -> bool:
    if getattr(booking, "object_type", None) != "group":
        return False
    if normalize_booking_status(getattr(booking, "status", None), default="") not in BOOKING_PAYMENT_CONFIRMED_STATUSES:
        return False

    abonements = _matching_group_abonements_for_booking(db, booking)
    if not abonements:
        return False

    statuses = {
        normalize_abonement_status(getattr(row, "status", None), default="")
        for row in abonements
    }
    return bool(statuses) and statuses.issubset(_TERMINAL_ABONEMENT_STATUSES)


def _group_booking_occupies_seat(db, booking: BookingRequest) -> bool:
    if getattr(booking, "object_type", None) != "group":
        return False

    normalized_status = normalize_booking_status(getattr(booking, "status", None), default="")
    if normalized_status in BOOKING_RESERVATION_EXPIRABLE_STATUSES or normalized_status == "created":
        return True
    if normalized_status in BOOKING_PAYMENT_CONFIRMED_STATUSES:
        return not _is_stale_group_booking(db, booking)
    return False


def _cleanup_inactive_group_bookings(
    db,
    *,
    now: datetime,
    group_id: int | None = None,
    user_id: int | None = None,
    group_start_date: date | None = None,
) -> set[int]:
    query = db.query(BookingRequest).filter(
        BookingRequest.object_type == "group",
        BookingRequest.status.in_(tuple(BOOKING_PAYMENT_CONFIRMED_STATUSES)),
    )
    if group_id is not None:
        query = query.filter(BookingRequest.group_id == int(group_id))
    if user_id is not None:
        query = query.filter(BookingRequest.user_id == int(user_id))
    if group_start_date is None:
        query = query.filter(BookingRequest.group_start_date.is_(None))
    else:
        query = query.filter(BookingRequest.group_start_date == group_start_date)

    cancelled_ids: set[int] = set()
    for row in query.all():
        if not _is_stale_group_booking(db, row):
            continue
        set_booking_status(
            row,
            BOOKING_STATUS_CANCELLED,
            actor_name="system: abonement inactive",
            changed_at=now,
            allow_same=False,
        )
        row.reserved_until = None
        cancelled_ids.add(int(row.id))
    return cancelled_ids


def _cancel_replaceable_group_bookings(
    db,
    booking: BookingRequest,
    *,
    now: datetime,
) -> set[int]:
    if getattr(booking, "object_type", None) != "group":
        return set()
    if not getattr(booking, "user_id", None) or not getattr(booking, "group_id", None):
        return set()

    query = db.query(BookingRequest).filter(
        BookingRequest.user_id == int(booking.user_id),
        BookingRequest.object_type == "group",
        BookingRequest.group_id == int(booking.group_id),
        BookingRequest.status.in_((BOOKING_STATUS_CREATED, BOOKING_STATUS_WAITING_PAYMENT)),
    )
    if booking.group_start_date is None:
        query = query.filter(BookingRequest.group_start_date.is_(None))
    else:
        query = query.filter(BookingRequest.group_start_date == booking.group_start_date)

    cancelled_ids: set[int] = set()
    for row in query.all():
        row_status = normalize_booking_status(getattr(row, "status", None), default="")
        if row_status == BOOKING_STATUS_CREATED:
            row.status = BOOKING_STATUS_CANCELLED
            if hasattr(row, "status_updated_at"):
                row.status_updated_at = now
            if hasattr(row, "status_updated_by_name"):
                row.status_updated_by_name = "system: replaced by newer booking"
        elif row_status == BOOKING_STATUS_WAITING_PAYMENT:
            set_booking_status(
                row,
                BOOKING_STATUS_CANCELLED,
                actor_name="system: replaced by newer booking",
                changed_at=now,
                allow_same=False,
            )
        else:
            continue
        row.reserved_until = None
        if hasattr(row, "payment_deadline_alert_sent_at"):
            row.payment_deadline_alert_sent_at = None
        cancelled_ids.add(int(row.id))
    return cancelled_ids


def _has_duplicate_group_booking(db, booking: BookingRequest) -> bool:
    query = db.query(BookingRequest).filter(
        BookingRequest.user_id == int(booking.user_id),
        BookingRequest.object_type == "group",
        BookingRequest.group_id == int(booking.group_id),
        BookingRequest.status.in_(tuple(BOOKING_SEAT_OCCUPYING_STATUSES)),
    )
    if booking.group_start_date is None:
        query = query.filter(BookingRequest.group_start_date.is_(None))
    else:
        query = query.filter(BookingRequest.group_start_date == booking.group_start_date)
    return any(_group_booking_occupies_seat(db, row) for row in query.all())


def _count_group_occupied_seats(db, group_id: int) -> int:
    rows = (
        db.query(BookingRequest)
        .filter(
            BookingRequest.object_type == "group",
            BookingRequest.group_id == int(group_id),
            BookingRequest.status.in_(tuple(BOOKING_SEAT_OCCUPYING_STATUSES)),
        )
        .all()
    )
    return sum(1 for row in rows if _group_booking_occupies_seat(db, row))


def get_group_occupancy_map(db, group_ids: Iterable[int]) -> dict[int, int]:
    normalized_group_ids_set: set[int] = set()
    for raw_group_id in group_ids:
        try:
            normalized_group_id = int(raw_group_id)
        except (TypeError, ValueError):
            continue
        if normalized_group_id > 0:
            normalized_group_ids_set.add(normalized_group_id)
    normalized_group_ids = sorted(normalized_group_ids_set)
    if not normalized_group_ids:
        return {}

    rows = (
        db.query(BookingRequest)
        .filter(
            BookingRequest.object_type == "group",
            BookingRequest.group_id.in_(normalized_group_ids),
            BookingRequest.status.in_(tuple(BOOKING_SEAT_OCCUPYING_STATUSES)),
        )
        .all()
    )
    occupancy_map: dict[int, int] = {}
    for row in rows:
        if row.group_id is None or not _group_booking_occupies_seat(db, row):
            continue
        normalized_group_id = int(row.group_id)
        occupancy_map[normalized_group_id] = occupancy_map.get(normalized_group_id, 0) + 1
    return occupancy_map


def count_group_occupied_seats(db, group_id: int) -> int:
    return _count_group_occupied_seats(db, int(group_id))


def count_group_free_seats(db, group_id: int, *, max_students: int | None) -> int | None:
    try:
        capacity = int(max_students or 0)
    except (TypeError, ValueError):
        return None
    if capacity <= 0:
        return None
    occupied = _count_group_occupied_seats(db, int(group_id))
    return max(0, capacity - occupied)


def create_booking_request_with_guards(
    db,
    booking: BookingRequest,
    *,
    now: datetime | None = None,
    reserve_minutes: int = DEFAULT_GROUP_BOOKING_RESERVE_MINUTES,
) -> BookingRequest:
    current_time = now or datetime.utcnow()
    if not booking.user_id:
        raise ValueError("user_id is required")

    booking.status = normalize_booking_status(booking.status)
    booking.reserved_until = None

    object_type = str(booking.object_type or "").strip().lower()
    if object_type not in {"rental", "individual", "group"}:
        raise ValueError("object_type must be rental, individual, or group")

    if object_type == "group":
        if not booking.group_id:
            raise ValueError("group_id is required for group booking")
        group = (
            db.query(Group)
            .filter(Group.id == int(booking.group_id))
            .with_for_update()
            .first()
        )
        if not group:
            raise ValueError("Group not found")

        _cleanup_inactive_group_bookings(
            db,
            now=current_time,
            group_id=int(booking.group_id),
            group_start_date=booking.group_start_date,
        )
        _cancel_replaceable_group_bookings(
            db,
            booking,
            now=current_time,
        )
        expire_stale_booking_reservations(
            db,
            now=current_time,
            group_id=int(booking.group_id),
        )

        if _has_duplicate_group_booking(db, booking):
            raise BookingAlreadyExistsError("Клиент уже записан")

        max_students = int(group.max_students or 0)
        occupied = _count_group_occupied_seats(db, int(booking.group_id))
        if max_students <= 0 or occupied >= max_students:
            raise BookingCapacityExceededError("Свободных мест нет")
    else:
        if _has_duplicate_non_group_booking(db, booking):
            raise BookingAlreadyExistsError("Клиент уже записан")

    if booking.status == BOOKING_STATUS_WAITING_PAYMENT:
        booking.reserved_until = _reservation_deadline(current_time, reserve_minutes)

    db.add(booking)
    try:
        db.flush()
    except IntegrityError as exc:
        if _is_duplicate_booking_integrity_error(exc):
            raise BookingAlreadyExistsError("Клиент уже записан") from exc
        raise
    return booking


def _time_overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a

def _compute_duration_minutes(time_from, time_to) -> int | None:
    if not time_from or not time_to:
        return None
    delta = datetime.combine(date.today(), time_to) - datetime.combine(date.today(), time_from)
    minutes = int(delta.total_seconds() // 60)
    return minutes if minutes > 0 else None


def _to_int_or_none(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0:
        return None
    return parsed


def _resolve_bookings_admin_chat_id(booking: BookingRequest):
    try:
        from dance_studio.core.config import BOOKINGS_ADMIN_CHAT_ID
    except Exception:
        BOOKINGS_ADMIN_CHAT_ID = None

    fallback = _to_int_or_none(BOOKINGS_ADMIN_CHAT_ID)
    db = object_session(booking)
    if not db:
        return fallback

    try:
        configured = _to_int_or_none(get_setting_value(db, "bookings.admin_chat_id"))
        return configured if configured is not None else fallback
    except Exception:
        return fallback

def _find_booking_overlaps(db, date_val, time_from, time_to) -> list[dict]:
    overlaps = []

    schedules = db.query(Schedule).filter(
        Schedule.date == date_val,
        Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES))
    ).all()
    for item in schedules:
        start = item.time_from or item.start_time
        end = item.time_to or item.end_time
        if not start or not end:
            continue
        if _time_overlaps(time_from, time_to, start, end):
            overlaps.append({
                "date": date_val.strftime("%d.%m.%Y"),
                "time_from": start.strftime("%H:%M"),
                "time_to": end.strftime("%H:%M"),
                "title": item.title or "Занятие"
            })

    rentals = db.query(HallRental).filter_by(date=date_val).all()
    for item in rentals:
        if not item.time_from or not item.time_to:
            continue
        if _time_overlaps(time_from, time_to, item.time_from, item.time_to):
            overlaps.append({
                "date": date_val.strftime("%d.%m.%Y"),
                "time_from": item.time_from.strftime("%H:%M"),
                "time_to": item.time_to.strftime("%H:%M"),
                "title": "Аренда зала"
            })

    lessons = db.query(IndividualLesson).filter_by(date=date_val).all()
    for item in lessons:
        if not item.time_from or not item.time_to:
            continue
        if _time_overlaps(time_from, time_to, item.time_from, item.time_to):
            overlaps.append({
                "date": date_val.strftime("%d.%m.%Y"),
                "time_from": item.time_from.strftime("%H:%M"),
                "time_to": item.time_to.strftime("%H:%M"),
                "title": "ндивидуальное занятие"
            })

    return overlaps

def _notify_booking_admins(booking: BookingRequest, user: User) -> None:
    try:
        from dance_studio.core.config import BOT_TOKEN
    except Exception:
        return

    admin_chat_id = _resolve_bookings_admin_chat_id(booking)
    if not BOT_TOKEN or not admin_chat_id:
        return

    text = format_booking_message(booking, user)
    is_free_group_trial = (
        booking.object_type == "group"
        and (booking.abonement_type or "").strip().lower() == ABONEMENT_TYPE_TRIAL
        and int(booking.requested_amount or 0) == 0
    )
    keyboard_data = build_booking_keyboard_data(
        booking.status,
        booking.object_type,
        booking.id,
        is_free_group_trial=is_free_group_trial,
    )

    payload = {
        "chat_id": admin_chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if keyboard_data:
        payload["reply_markup"] = {"inline_keyboard": keyboard_data}

    telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(telegram_api_url, json=payload, timeout=5)
    except Exception:
        pass

def _compute_group_booking_payment_amount(db, booking: BookingRequest) -> int | None:
    if booking.object_type != "group":
        return None
    if booking.requested_amount is not None:
        try:
            amount = int(booking.requested_amount)
        except (TypeError, ValueError):
            return None
        return amount if amount >= 0 else None

    if not booking.group_id:
        return None
    try:
        quote = quote_group_booking(
            db,
            user_id=None,  # quote for already created booking should not be blocked by trial checks
            group_id=booking.group_id,
            abonement_type=booking.abonement_type or ABONEMENT_TYPE_MULTI,
            bundle_group_ids=parse_booking_bundle_group_ids(booking),
        )
    except AbonementPricingError:
        return None
    return quote.amount

def _build_booking_payment_request_message(db, booking: BookingRequest) -> str:
    profile = _resolve_payment_profile_payload_for_booking(db, booking) or {}
    bank = str(profile.get("recipient_bank") or "—").strip() or "—"
    number = str(profile.get("recipient_number") or "—").strip() or "—"
    full_name = str(profile.get("recipient_full_name") or "—").strip() or "—"

    amount = _compute_group_booking_payment_amount(db, booking)
    amount_text = f"{amount:,} ₽".replace(",", " ") if amount else "уточните у администратора"

    return (
        "Здравствуйте!\n"
        f"Это администрация {PROJECT_NAME_FULL} Studio.\n\n"
        "Реквизиты для оплаты:\n"
        f"• Банк получателя: {bank}\n"
        f"• Номер: {number}\n"
        f"• ФИО получателя: {full_name}\n"
        f"• Сумма к оплате: {amount_text}\n\n"
        "Пожалуйста, после оплаты отправьте чек для подтверждения в этот чат."
    )

def _humanize_userbot_error(raw_reason: str) -> str:
    reason = str(raw_reason or "").strip()
    if not reason:
        return "неизвестная ошибка"

    # Unwrap wrappers like "userbot returned: {...}" and keep the most specific error text.
    wrapped_match = re.search(r"userbot returned:\s*(.+)$", reason, flags=re.IGNORECASE)
    if wrapped_match:
        reason = wrapped_match.group(1).strip()

    dict_error_match = re.search(r"'error'\s*:\s*'([^']+)'", reason)
    if not dict_error_match:
        dict_error_match = re.search(r'"error"\s*:\s*"([^"]+)"', reason)
    if dict_error_match:
        reason = dict_error_match.group(1).strip()

    if reason in {"None", "null", "{}"}:
        return "userbot не вернул текст ошибки"

    # Specific Telethon/Telegram RPC code translations.
    allow_payment_match = re.search(r"\bALLOW_PAYMENT_REQUIRED_(\d+)\b", reason, flags=re.IGNORECASE)
    if allow_payment_match:
        stars = allow_payment_match.group(1)
        return f"Требуется {stars} звёзд Telegram для отправки сообщения (ALLOW_PAYMENT_REQUIRED_{stars})"

    known_codes = {
        "USER_IS_BLOCKED": "Пользователь запретил личные сообщения от аккаунта userbot",
        "CHAT_WRITE_FORBIDDEN": "Нет прав на отправку сообщения этому пользователю",
        "PEER_FLOOD": "Ограничение Telegram на частые действия (flood control)",
        "FLOOD_WAIT": "Telegram временно ограничил отправку сообщений (flood wait)",
        "PRIVACY_RESTRICTED": "Ограничения приватности пользователя не позволяют написать ему",
    }
    upper_reason = reason.upper()
    for code, text in known_codes.items():
        if code in upper_reason:
            return f"{text} ({code})"

    return reason


def _notify_user_userbot_delivery_failed(telegram_id: int, booking_id: int, reason: str) -> None:
    fallback_text = (
        "Не получилось отправить вам реквизиты через userbot.\n"
        "Пожалуйста, напишите в этот бот, и мы отправим реквизиты здесь."
    )
    context_note = (
        f"Ошибка отправки реквизитов через userbot: booking #{booking_id}; "
        f"причина: {reason}"
    )
    try:
        send_user_notification_sync(
            user_id=int(telegram_id),
            text=fallback_text,
            context_note=context_note,
        )
    except Exception:
        current_app.logger.exception(
            "booking %s: failed to notify user %s about userbot delivery issue",
            booking_id,
            telegram_id,
        )


def _send_booking_payment_details_via_userbot(db, booking: BookingRequest, user: User | None) -> None:
    telegram_id = user.telegram_id if user else booking.user_telegram_id
    if not telegram_id:
        current_app.logger.warning("booking %s: skip payment DM, telegram_id missing", booking.id)
        return

    try:
        from dance_studio.bot.telegram_userbot import send_private_message_sync
    except Exception as exc:
        current_app.logger.exception("booking %s: userbot import failed", booking.id)
        reason = _humanize_userbot_error(str(exc))
        _notify_user_userbot_delivery_failed(int(telegram_id), int(booking.id), reason)
        return

    payment_text = _build_booking_payment_request_message(db, booking)
    user_target = {
        "id": telegram_id,
        "username": user.username if user else booking.user_username,
        "phone": user.phone if user else None,
        "name": user.name if user else booking.user_name,
    }
    try:
        result = send_private_message_sync(user_target, payment_text)
        if not result:
            raise RuntimeError("userbot returned: None")
        if not result.get("ok"):
            detail = str(result.get("error") or "").strip()
            if detail:
                raise RuntimeError(detail)
            raise RuntimeError(f"userbot returned: {result!r}")
    except Exception as exc:
        current_app.logger.exception("booking %s: failed to deliver payment details via userbot", booking.id)
        reason = _humanize_userbot_error(str(exc))
        _notify_user_userbot_delivery_failed(int(telegram_id), int(booking.id), reason)
        try:
            from dance_studio.core.config import BOT_TOKEN
            admin_chat_id = _resolve_bookings_admin_chat_id(booking)

            if BOT_TOKEN and admin_chat_id:
                username = f"@{user_target['username']}" if user_target.get("username") else "—"
                alert_text = (
                    "⚠️ Не удалось отправить реквизиты через userbot.\n"
                    f"Заявка: #{booking.id}\n"
                    f"Получатель: {user_target.get('name') or 'пользователь'} "
                    f"(id={telegram_id}, username={username})\n"
                    f"Причина: {reason}"
                )
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": admin_chat_id, "text": alert_text},
                    timeout=5,
                )
        except Exception:
            pass

def get_next_group_date(db, group_id):
    return pricing_get_next_group_date(db, int(group_id))


__all__ = [
    "BookingAlreadyExistsError",
    "BookingCapacityExceededError",
    "BookingConstraintError",
    "BookingReservationExpiredError",
    "_compute_duration_minutes",
    "_find_booking_overlaps",
    "_notify_booking_admins",
    "_send_booking_payment_details_via_userbot",
    "count_group_free_seats",
    "count_group_occupied_seats",
    "create_booking_request_with_guards",
    "expire_stale_booking_reservations",
    "get_next_group_date",
    "get_group_occupancy_map",
    "is_booking_reservation_expired",
]
