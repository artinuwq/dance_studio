import html
import json
from datetime import datetime

BOOKING_STATUS_LABELS = {
    "NEW": "NEW — ожидает решения администратора",
    "APPROVED": "APPROVED — бронь подтверждена, оплата не получена",
    "AWAITING_PAYMENT": "AWAITING_PAYMENT — ожидаем оплату",
    "PAID": "PAID — заявка завершена",
    "REJECTED": "REJECTED — заявка отклонена",
    "CANCELLED": "CANCELLED — бронь отменена",
    "PAYMENT_FAILED": "PAYMENT_FAILED — оплата не получена",
}

BOOKING_TYPE_LABELS = {
    "rental": "Аренда зала",
    "individual": "Индивидуальное занятие",
    "group": "Групповое занятие",
}


def parse_overlaps(overlaps_json: str | None) -> list[dict]:
    if not overlaps_json:
        return []
    try:
        data = json.loads(overlaps_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def parse_bundle_group_ids(bundle_group_ids_json: str | None) -> list[int]:
    if not bundle_group_ids_json:
        return []
    try:
        payload = json.loads(bundle_group_ids_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    result: list[int] = []
    seen: set[int] = set()
    for item in payload:
        try:
            group_id = int(item)
        except (TypeError, ValueError):
            continue
        if group_id <= 0 or group_id in seen:
            continue
        seen.add(group_id)
        result.append(group_id)
    return result


def _format_date(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        return value
    return value.strftime("%d.%m.%Y")


def _format_time(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        return value
    return value.strftime("%H:%M")


def _format_duration(minutes: int | None) -> str:
    if not minutes:
        return ""
    hours = minutes // 60
    mins = minutes % 60
    parts = []
    if hours:
        if hours % 10 == 1 and hours % 100 != 11:
            suffix = ""
        elif 2 <= hours % 10 <= 4 and not (12 <= hours % 100 <= 14):
            suffix = "а"
        else:
            suffix = "ов"
        parts.append(f"{hours} час{suffix}")
    if mins:
        parts.append(f"{mins} мин")
    return " ".join(parts)


def format_overlap_lines(overlaps: list[dict]) -> list[str]:
    lines = []
    for item in overlaps:
        date_val = _format_date(item.get("date"))
        time_from = _format_time(item.get("time_from"))
        time_to = _format_time(item.get("time_to"))
        title = item.get("title") or "Занятие"
        safe_title = html.escape(str(title))
        lines.append(f"• {date_val} — {time_from}–{time_to} ({safe_title})")
    return lines


def format_booking_message(booking, user=None) -> str:
    user_name = booking.user_name or (user.name if user else None) or "—"
    username = booking.user_username or (user.username if user else None)
    telegram_id = booking.user_telegram_id or (user.telegram_id if user else None)

    safe_name = html.escape(str(user_name))
    username_display = f"@{html.escape(username)}" if username else "—"
    contact_links = []
    if username:
        contact_links.append(f"<a href=\"https://t.me/{html.escape(username)}\">@{html.escape(username)}</a>")
    if telegram_id:
        contact_links.append(f"<a href=\"tg://user?id={telegram_id}\">tg://user?id={telegram_id}</a>")
    contact_line = " • ".join(contact_links) if contact_links else "—"

    header = "🆕 НОВАЯ ЗАЯВКА" if booking.status == "NEW" else "📝 ЗАЯВКА"
    booking_type = BOOKING_TYPE_LABELS.get(booking.object_type, booking.object_type)

    time_section = ""
    if booking.date and booking.time_from and booking.time_to:
        duration = booking.duration_minutes
        duration_text = _format_duration(duration)
        duration_suffix = f" ({duration_text})" if duration_text else ""
        time_section = (
            "🗓 Время:\n"
            f"• Дата: {_format_date(booking.date)}\n"
        f"• С {_format_time(booking.time_from)} до {_format_time(booking.time_to)}{duration_suffix}\n\n"
    )

    comment_section = ""
    if booking.comment:
        comment_section = (
            "📝 Комментарий:\n"
            f"{html.escape(str(booking.comment))}\n\n"
        )

    overlaps_section = ""
    if booking.object_type != "group":
        overlaps = parse_overlaps(booking.overlaps_json)
        if overlaps:
            overlap_lines = "\n".join(format_overlap_lines(overlaps))
            overlaps_section = (
                "⚠️ Пересечения:\n"
                "⚠️ ПЕРЕСЕЧЕНИЯ ОБНАРУЖЕНЫ\n\n"
                f"{overlap_lines}\n\n"
                "Рекомендуется проверить перед подтверждением.\n\n"
            )
        else:
            overlaps_section = (
                "⚠️ Пересечения:\n"
                "✅ Пересечений не обнаружено\n\n"
            )

    status_line = BOOKING_STATUS_LABELS.get(booking.status, booking.status)
    status_section = f"📌 Статус:\n{status_line}\n"

    admin_section = ""
    if booking.status_updated_at:
        admin_title = "👤 Подтвердил:" if booking.status == "PAID" else "👤 Администратор:"
        admin_name = booking.status_updated_by_username or booking.status_updated_by_name or "—"
        safe_admin = html.escape(str(admin_name))
        admin_time = booking.status_updated_at.strftime("%d.%m.%Y %H:%M")
        admin_section = (
            "\n"
            f"{admin_title}\n"
            f"• {safe_admin}\n"
            f"• {admin_time}\n"
        )

    lesson_section = ""
    if booking.object_type == "group":
        lesson_lines = []
        abonement_type_labels = {
            "single": "Разовое",
            "multi": "Многоразовое",
            "trial": "Пробное",
        }
        abonement_type = str(getattr(booking, "abonement_type", "") or "").strip().lower()
        if abonement_type:
            lesson_lines.append(f"• Тип абонемента: {abonement_type_labels.get(abonement_type, abonement_type)}")

        bundle_group_ids = parse_bundle_group_ids(getattr(booking, "bundle_group_ids_json", None))
        if bundle_group_ids:
            lesson_lines.append(f"• Размер пакета: {len(bundle_group_ids)}")
            lesson_lines.append(f"• Группы пакета (ID): {', '.join(map(str, bundle_group_ids))}")
        group = getattr(booking, "group", None)
        if group and group.name:
            lesson_lines.append(f"• Группа: {html.escape(group.name)}")
        direction = getattr(group, "direction", None) if group else None
        if direction and direction.title:
            lesson_lines.append(f"• Направление: {html.escape(direction.title)}")
            if direction.base_price:
                lesson_lines.append(f"• Цена занятия: {direction.base_price} ₽")
        if group and group.teacher and group.teacher.name:
            lesson_lines.append(f"• Преподаватель: {html.escape(group.teacher.name)}")
        if group and group.age_group:
            lesson_lines.append(f"• Возраст: {html.escape(group.age_group)}")
        if group and group.lessons_per_week:
            lesson_lines.append(f"• {group.lessons_per_week} занятий в неделю")
        if booking.lessons_count:
            lesson_lines.append(f"• Кол-во занятий: {booking.lessons_count}")
        if booking.group_start_date:
            lesson_lines.append(f"• Следующее занятие: {_format_date(booking.group_start_date)}")
        if booking.valid_until:
            lesson_lines.append(f"• Абонемент действует до: {_format_date(booking.valid_until)}")
        if getattr(booking, "requested_amount", None) is not None:
            currency = getattr(booking, "requested_currency", None) or "RUB"
            lesson_lines.append(f"• К оплате: {booking.requested_amount} {currency}")
        if lesson_lines:
            lesson_section = "🎯 О занятии:\n" + "\n".join(lesson_lines) + "\n\n"

    elif booking.object_type == "individual":
        teacher = getattr(booking, "teacher", None)
        if teacher and teacher.name:
            lesson_section = (
                "🎯 Преподаватель:\n"
                f"• {html.escape(teacher.name)}\n"
            )
            if teacher.specialization:
                lesson_section += f"• {html.escape(teacher.specialization)}\n"
            lesson_section += "\n"

    return (
        f"{header}\n\n"
        "👤 Клиент:\n"
        f"• Имя: {safe_name}\n"
        f"• Username: {username_display}\n"
        f"• Написать: {contact_line}\n\n"
        "📦 Тип:\n"
        f"{booking_type}\n\n"
        f"{time_section}"
        f"{lesson_section}"
        f"{comment_section}"
        f"{overlaps_section}"
        f"{status_section}"
        f"{admin_section}"
    )


def build_booking_keyboard_data(
    status: str,
    object_type: str,
    booking_id: int,
    *,
    is_free_group_trial: bool = False,
) -> list[list[dict]]:
    normalized_status = str(status or "").strip().lower()
    normalized_status = {
        "new": "created",
        "approved": "waiting_payment",
        "awaiting_payment": "waiting_payment",
        "paid": "confirmed",
        "rejected": "cancelled",
        "payment_failed": "cancelled",
    }.get(normalized_status, normalized_status)

    if object_type == "group":
        if normalized_status == "created":
            if is_free_group_trial:
                return [[{"text": "✅ Подтвердить", "callback_data": f"booking:{booking_id}:approve"}]]
            return [[{"text": "✅ Запросить оплату", "callback_data": f"booking:{booking_id}:request_payment"}]]
        if normalized_status == "waiting_payment":
            return [[{"text": "✅ Подтвердить оплату", "callback_data": f"booking:{booking_id}:confirm_payment"}]]
        return []

    if normalized_status == "created":
        return [[{"text": "✅ Запросить оплату", "callback_data": f"booking:{booking_id}:request_payment"}]]
    if normalized_status == "waiting_payment":
        return [[{"text": "✅ Подтвердить оплату", "callback_data": f"booking:{booking_id}:confirm_payment"}]]
    return []


