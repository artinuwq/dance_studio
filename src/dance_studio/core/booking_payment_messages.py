# -*- coding: utf-8 -*-
from __future__ import annotations

from dance_studio.core.abonement_pricing import parse_booking_bundle_group_ids
from dance_studio.db.models import BookingRequest, Group

_EM_DASH = "\u2014"
_GROUP_FALLBACK_PREFIX = "\u0413\u0440\u0443\u043f\u043f\u0430"  # Ð“Ñ€ÑƒÐ¿Ð¿Ð°
_GROUP_SUBJECT = "\u0410\u0431\u043e\u043d\u0435\u043c\u0435\u043d\u0442:"  # ÐÐ±Ð¾Ð½ÐµÐ¼ÐµÐ½Ñ‚:
_RENTAL_SUBJECT = "\u0410\u0440\u0435\u043d\u0434\u0430:"  # ÐÑ€ÐµÐ½Ð´Ð°:
_INDIVIDUAL_SUBJECT = (
    "\u0418\u043d\u0434\u0438\u0432\u0438\u0434\u0443\u0430\u043b\u044c\u043d\u043e\u0435 \u0437\u0430\u043d\u044f\u0442\u0438\u0435:"
)  # Ð˜Ð½Ð´Ð¸Ð²Ð¸Ð´ÑƒÐ°Ð»ÑŒÐ½Ð¾Ðµ Ð·Ð°Ð½ÑÑ‚Ð¸Ðµ:
_LABEL_DATE = "\u0414\u0430\u0442\u0430"  # Ð”Ð°Ñ‚Ð°
_LABEL_TIME = "\u0412\u0440\u0435\u043c\u044f"  # Ð’Ñ€ÐµÐ¼Ñ
_LABEL_TIME_FROM = "\u0412\u0440\u0435\u043c\u044f \u0441"  # Ð’Ñ€ÐµÐ¼Ñ Ñ
_LABEL_TIME_TO = "\u0412\u0440\u0435\u043c\u044f \u0434\u043e"  # Ð’Ñ€ÐµÐ¼Ñ Ð´Ð¾


def _format_date(value) -> str:
    if not value:
        return _EM_DASH
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or _EM_DASH
    return value.strftime("%d.%m.%Y")


def _format_time(value) -> str:
    if not value:
        return _EM_DASH
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or _EM_DASH
    return value.strftime("%H:%M")


def _resolve_group_names(db, booking: BookingRequest) -> list[str]:
    group_ids = parse_booking_bundle_group_ids(booking)
    if not group_ids:
        group = getattr(booking, "group", None)
        group_name = str(getattr(group, "name", "") or "").strip()
        return [group_name] if group_name else []

    groups_by_id: dict[int, object] = {}

    group = getattr(booking, "group", None)
    try:
        group_id = int(getattr(group, "id", 0) or 0)
    except (TypeError, ValueError):
        group_id = 0

    if group_id > 0:
        groups_by_id[group_id] = group

    if db is not None:
        missing_ids = [gid for gid in group_ids if gid not in groups_by_id]
        if missing_ids:
            rows = db.query(Group).filter(Group.id.in_(missing_ids)).all()
            for row in rows:
                try:
                    row_id = int(getattr(row, "id", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if row_id > 0:
                    groups_by_id[row_id] = row

    group_names: list[str] = []
    for group_id in group_ids:
        row = groups_by_id.get(group_id)
        group_name = str(getattr(row, "name", "") or "").strip()
        group_names.append(group_name or f"{_GROUP_FALLBACK_PREFIX} #{group_id}")
    return group_names


def build_booking_payment_subject_text(db, booking: BookingRequest) -> str:
    object_type = str(getattr(booking, "object_type", "") or "").strip().lower()

    if object_type == "group":
        lines = [_GROUP_SUBJECT]
        lines.extend(f"â€¢ {group_name}" for group_name in _resolve_group_names(db, booking))
        return "\n".join(lines)

    if object_type == "rental":
        lines = [_RENTAL_SUBJECT]
        if getattr(booking, "date", None):
            lines.append(f"â€¢ {_LABEL_DATE}: {_format_date(booking.date)}")
        time_from = getattr(booking, "time_from", None)
        time_to = getattr(booking, "time_to", None)

        if time_from and time_to:
            lines.append(f"â€¢ {_LABEL_TIME}: {_format_time(time_from)}-{_format_time(time_to)}")
        elif time_from:
            lines.append(f"â€¢ {_LABEL_TIME_FROM}: {_format_time(time_from)}")
        elif time_to:
            lines.append(f"â€¢ {_LABEL_TIME_TO}: {_format_time(time_to)}")
        return "\n".join(lines)

    if object_type == "individual":
        lines = [_INDIVIDUAL_SUBJECT]
        if getattr(booking, "date", None):
            lines.append(f"â€¢ {_LABEL_DATE}: {_format_date(booking.date)}")
        time_from = getattr(booking, "time_from", None)
        time_to = getattr(booking, "time_to", None)

        if time_from and time_to:
            lines.append(f"â€¢ {_LABEL_TIME}: {_format_time(time_from)}-{_format_time(time_to)}")
        elif time_from:
            lines.append(f"â€¢ {_LABEL_TIME_FROM}: {_format_time(time_from)}")
        elif time_to:
            lines.append(f"â€¢ {_LABEL_TIME_TO}: {_format_time(time_to)}")
        return "\n".join(lines)

    return ""


__all__ = ["build_booking_payment_subject_text"]