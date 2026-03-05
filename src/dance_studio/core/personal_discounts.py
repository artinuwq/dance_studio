from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from dance_studio.db.models import UserDiscount


@dataclass(frozen=True)
class PersonalDiscountApplication:
    amount_before_discount: int
    discount_amount: int
    final_amount: int
    discount_id: int | None
    discount_type: str | None
    discount_value: int | None
    is_one_time: bool | None


class DiscountConsumptionConflictError(RuntimeError):
    pass


def _to_non_negative_int(raw_value: Any) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _discount_amount_for(base_amount: int, discount_type: str | None, value: Any) -> int:
    if base_amount <= 0:
        return 0
    normalized_type = str(discount_type or "").strip().lower()
    normalized_value = _to_non_negative_int(value)
    if normalized_value <= 0:
        return 0
    if normalized_type == "percentage":
        amount = int(base_amount * (normalized_value / 100))
    elif normalized_type == "fixed":
        amount = normalized_value
    else:
        return 0
    return max(0, min(base_amount, int(amount)))


def apply_best_discount(base_amount: Any, discounts: Sequence[Any] | None) -> PersonalDiscountApplication:
    normalized_amount = _to_non_negative_int(base_amount)
    best_row = None
    best_score = None
    best_discount_amount = 0

    for row in discounts or []:
        if not bool(getattr(row, "is_active", False)):
            continue

        discount_amount = _discount_amount_for(
            normalized_amount,
            getattr(row, "discount_type", None),
            getattr(row, "value", None),
        )
        if discount_amount <= 0:
            continue

        is_one_time = bool(getattr(row, "is_one_time", False))
        created_at = getattr(row, "created_at", None) or datetime.min
        row_id = _to_non_negative_int(getattr(row, "id", 0))
        score = (
            discount_amount,
            1 if not is_one_time else 0,  # tie-break: reusable first
            created_at,
            row_id,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_row = row
            best_discount_amount = discount_amount

    if not best_row:
        return PersonalDiscountApplication(
            amount_before_discount=normalized_amount,
            discount_amount=0,
            final_amount=normalized_amount,
            discount_id=None,
            discount_type=None,
            discount_value=None,
            is_one_time=None,
        )

    final_amount = max(0, normalized_amount - best_discount_amount)
    return PersonalDiscountApplication(
        amount_before_discount=normalized_amount,
        discount_amount=best_discount_amount,
        final_amount=final_amount,
        discount_id=_to_non_negative_int(getattr(best_row, "id", 0)) or None,
        discount_type=str(getattr(best_row, "discount_type", "") or "").strip().lower() or None,
        discount_value=_to_non_negative_int(getattr(best_row, "value", 0)) or None,
        is_one_time=bool(getattr(best_row, "is_one_time", False)),
    )


def apply_best_discount_for_user(
    db,
    *,
    user_id: int | None,
    base_amount: Any,
) -> PersonalDiscountApplication:
    if not user_id:
        return apply_best_discount(base_amount, [])

    active_rows = (
        db.query(UserDiscount)
        .filter(
            UserDiscount.user_id == int(user_id),
            UserDiscount.is_active.is_(True),
        )
        .order_by(UserDiscount.created_at.desc(), UserDiscount.id.desc())
        .all()
    )
    return apply_best_discount(base_amount, active_rows)


def serialize_applied_discount(application: PersonalDiscountApplication) -> dict[str, Any] | None:
    if not application.discount_id:
        return None
    return {
        "discount_id": application.discount_id,
        "discount_type": application.discount_type,
        "discount_value": application.discount_value,
        "is_one_time": application.is_one_time,
        "discount_amount": application.discount_amount,
    }


def resolve_discount_usage_state(discount: Any) -> str:
    if bool(getattr(discount, "is_one_time", False)) and getattr(discount, "consumed_booking_id", None):
        return "consumed"
    if bool(getattr(discount, "is_active", False)):
        return "active"
    return "inactive"


def consume_one_time_discount_for_booking(
    db,
    *,
    booking: Any,
    consumed_at: datetime | None = None,
) -> bool:
    discount_id = _to_non_negative_int(getattr(booking, "applied_discount_id", None))
    user_id = _to_non_negative_int(getattr(booking, "user_id", None))
    booking_id = _to_non_negative_int(getattr(booking, "id", None))
    if discount_id <= 0 or user_id <= 0 or booking_id <= 0:
        return False

    discount = (
        db.query(UserDiscount)
        .filter(
            UserDiscount.id == discount_id,
            UserDiscount.user_id == user_id,
        )
        .first()
    )
    if not discount:
        raise DiscountConsumptionConflictError("Applied discount not found.")

    if not bool(discount.is_one_time):
        return False

    if not bool(discount.is_active):
        if _to_non_negative_int(discount.consumed_booking_id) == booking_id:
            return False
        raise DiscountConsumptionConflictError("One-time discount is already consumed.")

    now = consumed_at or datetime.now()
    updated = (
        db.query(UserDiscount)
        .filter(
            UserDiscount.id == discount_id,
            UserDiscount.user_id == user_id,
            UserDiscount.is_one_time.is_(True),
            UserDiscount.is_active.is_(True),
        )
        .update(
            {
                "is_active": False,
                "consumed_at": now,
                "consumed_booking_id": booking_id,
            },
            synchronize_session=False,
        )
    )
    if updated == 1:
        return True

    # Race-safe recheck for idempotent repeated consume on the same booking.
    refreshed = (
        db.query(UserDiscount)
        .filter(UserDiscount.id == discount_id, UserDiscount.user_id == user_id)
        .first()
    )
    if refreshed and _to_non_negative_int(refreshed.consumed_booking_id) == booking_id:
        return False
    raise DiscountConsumptionConflictError("One-time discount is already consumed.")
