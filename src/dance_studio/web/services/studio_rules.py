from __future__ import annotations

from datetime import date, time

PRIMARY_OWNER_KEY = "primary_owner"
SECONDARY_OWNER_KEY = "secondary_owner"

SERVICE_BREAK_START = time(14, 30)
SERVICE_BREAK_END = time(15, 0)

# Monday, Wednesday, Friday: morning belongs to secondary owner.
SECONDARY_OWNER_MORNING_WEEKDAYS = {0, 2, 4}


def interval_overlaps_service_break(time_from: time | None, time_to: time | None) -> bool:
    if not time_from or not time_to or time_from >= time_to:
        return False
    return time_from < SERVICE_BREAK_END and SERVICE_BREAK_START < time_to


def owner_for_interval(
    booking_date: date | None,
    time_from: time | None,
    time_to: time | None,
) -> str | None:
    if not booking_date or not time_from or not time_to or time_from >= time_to:
        return None

    if interval_overlaps_service_break(time_from, time_to):
        return None

    weekday = int(booking_date.weekday())
    morning_is_secondary = weekday in SECONDARY_OWNER_MORNING_WEEKDAYS

    if time_to <= SERVICE_BREAK_START:
        return SECONDARY_OWNER_KEY if morning_is_secondary else PRIMARY_OWNER_KEY
    if time_from >= SERVICE_BREAK_END:
        return PRIMARY_OWNER_KEY if morning_is_secondary else SECONDARY_OWNER_KEY

    return None


def owner_for_group_direction(direction_type: str | None) -> str | None:
    normalized = str(direction_type or "").strip().lower()
    if normalized == "sport":
        return PRIMARY_OWNER_KEY
    if normalized == "dance":
        return SECONDARY_OWNER_KEY
    return None


__all__ = [
    "PRIMARY_OWNER_KEY",
    "SECONDARY_OWNER_KEY",
    "SERVICE_BREAK_START",
    "SERVICE_BREAK_END",
    "interval_overlaps_service_break",
    "owner_for_group_direction",
    "owner_for_interval",
]
