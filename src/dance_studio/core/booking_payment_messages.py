# -*- coding: utf-8 -*-
from __future__ import annotations

from dance_studio.core.abonement_pricing import parse_booking_bundle_group_ids
from dance_studio.db.models import BookingRequest, Group


def _format_date(value) -> str:
    if not value:
        return "Ч"
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or "Ч"
    return value.strftime("%d.%m.%Y")


def _format_time(value) -> str:
    if not value:
        return "Ч"
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or "Ч"
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
    for gid in group_ids:
        row = groups_by_id.get(gid)
        name = str(getattr(row, "name", "") or "").strip()
        group_names.append(name or f"√руппа #{gid}")

    return group_names


def build_booking_payment_subject_text(db, booking: BookingRequest) -> str:
    object_type = str(getattr(booking, "object_type", "") or "").strip().lower()

    if object_type == "group":
        lines = ["јбонемент:"]
        lines.extend(f"Х {name}" for name in _resolve_group_names(db, booking))
        return "\n".join(lines)

    if object_type == "rental":
        lines = ["јренда:"]
        if getattr(booking, "date", None):
            lines.append(f"Х ƒата: {_format_date(booking.date)}")

        time_from = getattr(booking, "time_from", None)
        time_to = getattr(booking, "time_to", None)

        if time_from and time_to:
            lines.append(f"Х ¬рем€: {_format_time(time_from)}Ц{_format_time(time_to)}")
        elif time_from:
            lines.append(f"Х ¬рем€ с: {_format_time(time_from)}")
        elif time_to:
            lines.append(f"Х ¬рем€ до: {_format_time(time_to)}")

        return "\n".join(lines)

    if object_type == "individual":
        lines = ["»ндивидуальное зан€тие:"]
        if getattr(booking, "date", None):
            lines.append(f"Х ƒата: {_format_date(booking.date)}")

        time_from = getattr(booking, "time_from", None)
        time_to = getattr(booking, "time_to", None)

        if time_from and time_to:
            lines.append(f"Х ¬рем€: {_format_time(time_from)}Ц{_format_time(time_to)}")
        elif time_from:
            lines.append(f"Х ¬рем€ с: {_format_time(time_from)}")
        elif time_to:
            lines.append(f"Х ¬рем€ до: {_format_time(time_to)}")

        return "\n".join(lines)

    return ""


__all__ = ["build_booking_payment_subject_text"]