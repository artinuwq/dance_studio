import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import or_

from dance_studio.core.system_settings_service import get_setting_value
from dance_studio.db.models import BookingRequest, Direction, Group, GroupAbonement, Schedule


ABONEMENT_TYPE_SINGLE = "single"
ABONEMENT_TYPE_MULTI = "multi"
ABONEMENT_TYPE_TRIAL = "trial"
ALLOWED_ABONEMENT_TYPES = {
    ABONEMENT_TYPE_SINGLE,
    ABONEMENT_TYPE_MULTI,
    ABONEMENT_TYPE_TRIAL,
}
ALLOWED_DIRECTION_TYPES = {"dance", "sport"}
INACTIVE_GROUP_SCHEDULE_STATUSES = {
    "cancelled",
    "deleted",
    "rejected",
    "payment_failed",
    "CANCELLED",
    "DELETED",
    "REJECTED",
    "PAYMENT_FAILED",
}
DEFAULT_MULTI_SINGLE_PRICE_RUB = 400


class AbonementPricingError(ValueError):
    pass


@dataclass(frozen=True)
class GroupBookingQuote:
    group_id: int
    abonement_type: str
    bundle_group_ids: list[int]
    bundle_size: int
    direction_type: str
    lessons_per_group: int
    total_lessons: int
    amount: int
    currency: str
    valid_from: datetime
    valid_to: datetime
    requires_payment: bool


def _normalize_direction_type(raw_value: Any) -> str:
    direction_type = str(raw_value or "").strip().lower()
    if direction_type not in ALLOWED_DIRECTION_TYPES:
        raise AbonementPricingError("Direction type must be dance or sport.")
    return direction_type


def _normalize_abonement_type(raw_value: Any) -> str:
    abonement_type = str(raw_value or "").strip().lower()
    if abonement_type not in ALLOWED_ABONEMENT_TYPES:
        raise AbonementPricingError("abonement_type must be single, multi, or trial.")
    return abonement_type


def _normalize_multi_lessons_per_group(raw_value: Any) -> int | None:
    if raw_value in (None, ""):
        return None
    try:
        lessons_per_group = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise AbonementPricingError("multi_lessons_per_group must be an integer.") from exc
    if lessons_per_group not in {4, 8, 12}:
        raise AbonementPricingError("multi_lessons_per_group must be one of: 4, 8, 12.")
    return lessons_per_group


def _normalize_group_id(raw_value: Any, field_name: str) -> int:
    try:
        group_id = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise AbonementPricingError(f"{field_name} must be an integer.") from exc
    if group_id <= 0:
        raise AbonementPricingError(f"{field_name} must be > 0.")
    return group_id


def normalize_bundle_group_ids(group_id: int, raw_bundle_group_ids: Any) -> list[int]:
    if raw_bundle_group_ids in (None, ""):
        return [group_id]
    if not isinstance(raw_bundle_group_ids, (list, tuple)):
        raise AbonementPricingError("bundle_group_ids must be an array of integers.")

    result: list[int] = []
    seen: set[int] = set()
    for index, raw_value in enumerate(raw_bundle_group_ids, start=1):
        item_id = _normalize_group_id(raw_value, f"bundle_group_ids[{index}]")
        if item_id in seen:
            raise AbonementPricingError("bundle_group_ids must contain unique group ids.")
        seen.add(item_id)
        result.append(item_id)

    if not result:
        raise AbonementPricingError("bundle_group_ids must not be empty.")
    if group_id not in seen:
        raise AbonementPricingError("group_id must be included in bundle_group_ids.")
    return result


def _get_group_payloads(db, group_ids: list[int]) -> list[dict[str, Any]]:
    groups = db.query(Group).filter(Group.id.in_(group_ids)).all()
    by_id = {row.id: row for row in groups}
    missing_ids = [group_id for group_id in group_ids if group_id not in by_id]
    if missing_ids:
        raise AbonementPricingError(f"Group not found: {', '.join(map(str, missing_ids))}.")

    direction_ids = {row.direction_id for row in groups if row.direction_id}
    directions = db.query(Direction).filter(Direction.direction_id.in_(direction_ids)).all() if direction_ids else []
    directions_by_id = {row.direction_id: row for row in directions}

    payloads: list[dict[str, Any]] = []
    for group_id in group_ids:
        group = by_id[group_id]
        direction = directions_by_id.get(group.direction_id)
        if not direction:
            raise AbonementPricingError(f"Direction not found for group {group_id}.")

        lessons_per_week = None
        if group.lessons_per_week not in (None, ""):
            try:
                lessons_per_week = int(group.lessons_per_week)
            except (TypeError, ValueError):
                lessons_per_week = None

        payloads.append(
            {
                "group_id": group.id,
                "group_name": group.name,
                "direction_type": _normalize_direction_type(direction.direction_type),
                "lessons_per_week": lessons_per_week,
            }
        )
    return payloads


def get_next_group_date(db, group_id: int):
    today = datetime.now().date()
    item = (
        db.query(Schedule)
        .filter(
            Schedule.object_type == "group",
            Schedule.date.isnot(None),
            Schedule.status.notin_(list(INACTIVE_GROUP_SCHEDULE_STATUSES)),
            or_(Schedule.group_id == group_id, Schedule.object_id == group_id),
            Schedule.date >= today,
        )
        .order_by(Schedule.date.asc())
        .first()
    )
    if item and item.date:
        return item.date
    return today


def _get_json_price(settings_payload: Any, *keys: str) -> int | None:
    node = settings_payload
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(str(key))
    if node is None:
        return None
    try:
        amount = int(node)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return amount


def _resolve_multi_single_amount_with_fallback(
    db,
    *,
    direction_type: str,
    lessons_per_group: int,
) -> int:
    single_matrix = get_setting_value(db, "abonements.multi_single_prices_json")
    amount = _get_json_price(single_matrix, direction_type, str(lessons_per_group))
    if amount is not None:
        return amount

    # Fallback #1: derive single-group price from double-bundle matrix (half of 2-group package).
    bundle_matrix = get_setting_value(db, "abonements.multi_bundle_prices_json")
    bundle_two_amount = _get_json_price(bundle_matrix, direction_type, "2", str(lessons_per_group))
    if bundle_two_amount is not None:
        return max(0, int(bundle_two_amount) // 2)

    # Fallback #2: minimal safety fallback so UI can still show purchasable option.
    return DEFAULT_MULTI_SINGLE_PRICE_RUB


def _has_trial_for_direction_type(db, user_id: int, direction_type: str) -> bool:
    trial_group_ids = [
        group_id
        for (group_id,) in db.query(GroupAbonement.group_id).filter(
            GroupAbonement.user_id == user_id,
            GroupAbonement.abonement_type == ABONEMENT_TYPE_TRIAL,
        )
    ]
    if not trial_group_ids:
        return False

    rows = (
        db.query(Direction.direction_type)
        .join(Group, Group.direction_id == Direction.direction_id)
        .filter(Group.id.in_(trial_group_ids))
        .all()
    )
    for (row_type,) in rows:
        try:
            if _normalize_direction_type(row_type) == direction_type:
                return True
        except AbonementPricingError:
            continue
    return False


def _resolve_multi_lessons_per_group(
    bundle_payloads: list[dict[str, Any]],
    requested_lessons_per_group: int | None = None,
) -> int:
    lessons_per_week_values = [item.get("lessons_per_week") for item in bundle_payloads]
    if any(val is None for val in lessons_per_week_values):
        raise AbonementPricingError("lessons_per_week must be configured for all selected groups.")
    if any(val not in {1, 2, 3} for val in lessons_per_week_values):
        raise AbonementPricingError("lessons_per_week must be 1, 2, or 3 for multi abonement.")
    if len(set(lessons_per_week_values)) != 1:
        raise AbonementPricingError("All groups in bundle must have the same lessons_per_week.")

    max_lessons_per_group = int(lessons_per_week_values[0]) * 4
    if requested_lessons_per_group is None:
        return max_lessons_per_group
    if requested_lessons_per_group > max_lessons_per_group:
        raise AbonementPricingError(
            f"multi_lessons_per_group cannot exceed {max_lessons_per_group} for selected groups."
        )
    return requested_lessons_per_group


def quote_group_booking(
    db,
    *,
    user_id: int | None,
    group_id: Any,
    abonement_type: Any,
    bundle_group_ids: Any = None,
    multi_lessons_per_group: Any = None,
) -> GroupBookingQuote:
    main_group_id = _normalize_group_id(group_id, "group_id")
    normalized_type = _normalize_abonement_type(abonement_type)
    normalized_multi_lessons_per_group = _normalize_multi_lessons_per_group(multi_lessons_per_group)
    normalized_bundle_ids = normalize_bundle_group_ids(main_group_id, bundle_group_ids)
    bundle_size = len(normalized_bundle_ids)

    bundle_payloads = _get_group_payloads(db, normalized_bundle_ids)
    direction_type = bundle_payloads[0]["direction_type"]

    for payload in bundle_payloads:
        if payload["direction_type"] != direction_type:
            raise AbonementPricingError("Cannot mix sport and dance groups in one abonement bundle.")

    if normalized_type in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_TRIAL}:
        if bundle_size != 1:
            raise AbonementPricingError("Single and trial abonements can contain exactly one group.")
        lessons_per_group = 1
    else:
        if bundle_size < 1 or bundle_size > 3:
            raise AbonementPricingError("Multi abonement supports only 1, 2, or 3 groups.")
        lessons_per_group = _resolve_multi_lessons_per_group(
            bundle_payloads,
            requested_lessons_per_group=normalized_multi_lessons_per_group,
        )

    if normalized_type == ABONEMENT_TYPE_TRIAL and user_id:
        if _has_trial_for_direction_type(db, int(user_id), direction_type):
            raise AbonementPricingError("Trial abonement is already used for this direction type.")

    if normalized_type == ABONEMENT_TYPE_SINGLE:
        amount = int(get_setting_value(db, "abonements.single_visit_price_rub"))
    elif normalized_type == ABONEMENT_TYPE_TRIAL:
        amount = int(get_setting_value(db, "abonements.trial_price_rub"))
    else:
        if bundle_size == 1:
            amount = _resolve_multi_single_amount_with_fallback(
                db,
                direction_type=direction_type,
                lessons_per_group=lessons_per_group,
            )
        else:
            bundle_matrix = get_setting_value(db, "abonements.multi_bundle_prices_json")
            amount = _get_json_price(bundle_matrix, direction_type, str(bundle_size), str(lessons_per_group))
            if amount is None:
                raise AbonementPricingError(
                    f"Multi bundle price is not configured for {direction_type}/{bundle_size}/{lessons_per_group}."
                )

    if amount < 0:
        raise AbonementPricingError("Calculated amount must be >= 0.")

    next_group_date = get_next_group_date(db, main_group_id)
    valid_from = datetime.combine(next_group_date, time.min)
    if normalized_type in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_TRIAL}:
        valid_to = datetime.combine(next_group_date, time.max)
    else:
        valid_to = datetime.combine(next_group_date + timedelta(days=28), time.max)

    total_lessons = lessons_per_group * bundle_size
    return GroupBookingQuote(
        group_id=main_group_id,
        abonement_type=normalized_type,
        bundle_group_ids=normalized_bundle_ids,
        bundle_size=bundle_size,
        direction_type=direction_type,
        lessons_per_group=lessons_per_group,
        total_lessons=total_lessons,
        amount=amount,
        currency="RUB",
        valid_from=valid_from,
        valid_to=valid_to,
        requires_payment=(amount > 0),
    )


def serialize_group_booking_quote(quote: GroupBookingQuote) -> dict[str, Any]:
    return {
        "group_id": quote.group_id,
        "abonement_type": quote.abonement_type,
        "bundle_group_ids": list(quote.bundle_group_ids),
        "bundle_size": quote.bundle_size,
        "direction_type": quote.direction_type,
        "lessons_per_group": quote.lessons_per_group,
        "total_lessons": quote.total_lessons,
        "amount": quote.amount,
        "currency": quote.currency,
        "valid_from": quote.valid_from.isoformat() if quote.valid_from else None,
        "valid_to": quote.valid_to.isoformat() if quote.valid_to else None,
        "requires_payment": quote.requires_payment,
    }


def parse_booking_bundle_group_ids(booking: BookingRequest) -> list[int]:
    group_ids: list[int] = []
    raw_json = getattr(booking, "bundle_group_ids_json", None)
    if raw_json:
        try:
            data = json.loads(raw_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            data = []
        if isinstance(data, list):
            seen: set[int] = set()
            for raw_item in data:
                try:
                    parsed = int(raw_item)
                except (TypeError, ValueError):
                    continue
                if parsed <= 0 or parsed in seen:
                    continue
                seen.add(parsed)
                group_ids.append(parsed)

    if getattr(booking, "group_id", None):
        main_group_id = int(booking.group_id)
        if main_group_id not in group_ids:
            group_ids.insert(0, main_group_id)

    return group_ids


def is_free_trial_booking(booking: BookingRequest) -> bool:
    if getattr(booking, "object_type", None) != "group":
        return False
    if (getattr(booking, "abonement_type", "") or "").lower() != ABONEMENT_TYPE_TRIAL:
        return False
    try:
        amount = int(getattr(booking, "requested_amount", 0) or 0)
    except (TypeError, ValueError):
        return False
    return amount == 0
