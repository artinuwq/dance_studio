from __future__ import annotations

import math
from typing import Any

from dance_studio.core.system_settings_service import get_setting_value


def _normalize_positive_int(raw_value: Any) -> int | None:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def compute_non_group_booking_base_amount(
    db,
    *,
    object_type: str | None,
    duration_minutes: Any,
) -> int | None:
    normalized_type = str(object_type or "").strip().lower()
    normalized_duration = _normalize_positive_int(duration_minutes)
    if normalized_duration is None:
        return None

    if normalized_type == "rental":
        hourly_rate = int(get_setting_value(db, "rental.base_hour_price_rub"))
    elif normalized_type == "individual":
        hourly_rate = int(get_setting_value(db, "individual.base_hour_price_rub"))
    else:
        return None

    if hourly_rate <= 0:
        return 0
    amount = math.ceil(hourly_rate * (normalized_duration / 60))
    return max(0, int(amount))
