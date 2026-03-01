from __future__ import annotations

import re
from datetime import date, datetime, time

import requests
from flask import current_app

from dance_studio.core.abonement_pricing import (
    ABONEMENT_TYPE_MULTI,
    ABONEMENT_TYPE_TRIAL,
    AbonementPricingError,
    get_next_group_date as pricing_get_next_group_date,
    parse_booking_bundle_group_ids,
    quote_group_booking,
)
from dance_studio.core.booking_utils import build_booking_keyboard_data, format_booking_message
from dance_studio.db.models import BookingRequest, HallRental, IndividualLesson, Schedule, User
from dance_studio.web.constants import INACTIVE_SCHEDULE_STATUSES
from dance_studio.web.services.payments import _get_active_payment_profile_payload

def _time_overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a

def _compute_duration_minutes(time_from, time_to) -> int | None:
    if not time_from or not time_to:
        return None
    delta = datetime.combine(date.today(), time_to) - datetime.combine(date.today(), time_from)
    minutes = int(delta.total_seconds() // 60)
    return minutes if minutes > 0 else None

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
        from dance_studio.core.config import BOT_TOKEN, BOOKINGS_ADMIN_CHAT_ID
    except Exception:
        return

    if not BOT_TOKEN or not BOOKINGS_ADMIN_CHAT_ID:
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
        "chat_id": BOOKINGS_ADMIN_CHAT_ID,
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
    profile = _get_active_payment_profile_payload(db) or {}
    bank = str(profile.get("recipient_bank") or "—").strip() or "—"
    number = str(profile.get("recipient_number") or "—").strip() or "—"
    full_name = str(profile.get("recipient_full_name") or "—").strip() or "—"

    amount = _compute_group_booking_payment_amount(db, booking)
    amount_text = f"{amount:,} ₽".replace(",", " ") if amount else "уточните у администратора"

    return (
        "Здравствуйте!\n"
        "Это администрация Shebba Sports x Lissa Dance Studio.\n\n"
        "Реквизиты для оплаты:\n"
        f"• Банк получателя: {bank}\n"
        f"• Номер: {number}\n"
        f"• ФИО получателя: {full_name}\n"
        f"• Сумма к оплате: {amount_text}\n\n"
        "Пожалуйста, после оплаты отправьте чек для подтверждения."
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

def _send_booking_payment_details_via_userbot(db, booking: BookingRequest, user: User | None) -> None:
    telegram_id = user.telegram_id if user else booking.user_telegram_id
    if not telegram_id:
        current_app.logger.warning("booking %s: skip payment DM, telegram_id missing", booking.id)
        return

    try:
        from dance_studio.bot.telegram_userbot import send_private_message_sync
    except Exception:
        current_app.logger.exception("booking %s: userbot import failed", booking.id)
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
        try:
            from dance_studio.core.config import BOT_TOKEN, BOOKINGS_ADMIN_CHAT_ID

            if BOT_TOKEN and BOOKINGS_ADMIN_CHAT_ID:
                username = f"@{user_target['username']}" if user_target.get("username") else "—"
                reason = _humanize_userbot_error(str(exc))
                alert_text = (
                    "⚠️ Не удалось отправить реквизиты через userbot.\n"
                    f"Заявка: #{booking.id}\n"
                    f"Получатель: {user_target.get('name') or 'пользователь'} "
                    f"(id={telegram_id}, username={username})\n"
                    f"Причина: {reason}"
                )
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": BOOKINGS_ADMIN_CHAT_ID, "text": alert_text},
                    timeout=5,
                )
        except Exception:
            pass

def get_next_group_date(db, group_id):
    return pricing_get_next_group_date(db, int(group_id))


__all__ = [
    "_compute_duration_minutes",
    "_find_booking_overlaps",
    "_notify_booking_admins",
    "_send_booking_payment_details_via_userbot",
    "get_next_group_date",
]
