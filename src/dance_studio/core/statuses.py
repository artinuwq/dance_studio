from __future__ import annotations

from datetime import datetime

from dance_studio.core.time import utcnow
from typing import Any


BOOKING_STATUS_CREATED = "created"
BOOKING_STATUS_WAITING_PAYMENT = "waiting_payment"
BOOKING_STATUS_CONFIRMED = "confirmed"
BOOKING_STATUS_CANCELLED = "cancelled"
BOOKING_STATUS_ATTENDED = "attended"
BOOKING_STATUS_NO_SHOW = "no_show"

BOOKING_ALLOWED_STATUSES = {
    BOOKING_STATUS_CREATED,
    BOOKING_STATUS_WAITING_PAYMENT,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_ATTENDED,
    BOOKING_STATUS_NO_SHOW,
}
BOOKING_PAYMENT_CONFIRMED_STATUSES = {
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_ATTENDED,
    BOOKING_STATUS_NO_SHOW,
}
BOOKING_NEGATIVE_STATUSES = {
    BOOKING_STATUS_CANCELLED,
}
BOOKING_ACTIVE_STATUSES = BOOKING_ALLOWED_STATUSES - BOOKING_NEGATIVE_STATUSES

BOOKING_STATUS_TRANSITIONS: dict[str, set[str]] = {
    BOOKING_STATUS_CREATED: {
        BOOKING_STATUS_WAITING_PAYMENT,
        BOOKING_STATUS_CONFIRMED,
        BOOKING_STATUS_CANCELLED,
    },
    BOOKING_STATUS_WAITING_PAYMENT: {
        BOOKING_STATUS_CONFIRMED,
        BOOKING_STATUS_CANCELLED,
    },
    BOOKING_STATUS_CONFIRMED: {
        BOOKING_STATUS_ATTENDED,
        BOOKING_STATUS_NO_SHOW,
        BOOKING_STATUS_CANCELLED,
    },
    BOOKING_STATUS_CANCELLED: set(),
    BOOKING_STATUS_ATTENDED: set(),
    BOOKING_STATUS_NO_SHOW: set(),
}


ABONEMENT_STATUS_PENDING_PAYMENT = "pending_payment"
ABONEMENT_STATUS_ACTIVE = "active"
ABONEMENT_STATUS_EXPIRED = "expired"
ABONEMENT_STATUS_CANCELLED = "cancelled"

ABONEMENT_ALLOWED_STATUSES = {
    ABONEMENT_STATUS_PENDING_PAYMENT,
    ABONEMENT_STATUS_ACTIVE,
    ABONEMENT_STATUS_EXPIRED,
    ABONEMENT_STATUS_CANCELLED,
}

ABONEMENT_STATUS_TRANSITIONS: dict[str, set[str]] = {
    ABONEMENT_STATUS_PENDING_PAYMENT: {ABONEMENT_STATUS_ACTIVE},
    ABONEMENT_STATUS_ACTIVE: {
        ABONEMENT_STATUS_EXPIRED,
        ABONEMENT_STATUS_CANCELLED,
    },
    ABONEMENT_STATUS_EXPIRED: set(),
    ABONEMENT_STATUS_CANCELLED: set(),
}


_LEGACY_BOOKING_STATUS_MAP = {
    "new": BOOKING_STATUS_CREATED,
    "created": BOOKING_STATUS_CREATED,
    "approved": BOOKING_STATUS_WAITING_PAYMENT,
    "awaiting_payment": BOOKING_STATUS_WAITING_PAYMENT,
    "waiting_payment": BOOKING_STATUS_WAITING_PAYMENT,
    "paid": BOOKING_STATUS_CONFIRMED,
    "confirmed": BOOKING_STATUS_CONFIRMED,
    "cancelled": BOOKING_STATUS_CANCELLED,
    "canceled": BOOKING_STATUS_CANCELLED,
    "rejected": BOOKING_STATUS_CANCELLED,
    "payment_failed": BOOKING_STATUS_CANCELLED,
    "attended": BOOKING_STATUS_ATTENDED,
    "no_show": BOOKING_STATUS_NO_SHOW,
}

_LEGACY_ABONEMENT_STATUS_MAP = {
    "pending_payment": ABONEMENT_STATUS_PENDING_PAYMENT,
    "pending_activation": ABONEMENT_STATUS_PENDING_PAYMENT,
    "pending": ABONEMENT_STATUS_PENDING_PAYMENT,
    "new": ABONEMENT_STATUS_PENDING_PAYMENT,
    "created": ABONEMENT_STATUS_PENDING_PAYMENT,
    "active": ABONEMENT_STATUS_ACTIVE,
    "expired": ABONEMENT_STATUS_EXPIRED,
    "inactive": ABONEMENT_STATUS_EXPIRED,
    "cancelled": ABONEMENT_STATUS_CANCELLED,
    "canceled": ABONEMENT_STATUS_CANCELLED,
    "rejected": ABONEMENT_STATUS_CANCELLED,
    "blocked": ABONEMENT_STATUS_CANCELLED,
}


def _norm(raw_value: Any) -> str:
    return str(raw_value or "").strip().lower()


def normalize_booking_status(raw_status: Any, *, default: str = BOOKING_STATUS_CREATED) -> str:
    normalized = _norm(raw_status)
    if not normalized:
        return default
    mapped = _LEGACY_BOOKING_STATUS_MAP.get(normalized, normalized)
    if mapped in BOOKING_ALLOWED_STATUSES:
        return mapped
    return default


def normalize_abonement_status(raw_status: Any, *, default: str = ABONEMENT_STATUS_PENDING_PAYMENT) -> str:
    normalized = _norm(raw_status)
    if not normalized:
        return default
    mapped = _LEGACY_ABONEMENT_STATUS_MAP.get(normalized, normalized)
    if mapped in ABONEMENT_ALLOWED_STATUSES:
        return mapped
    return default


def ensure_booking_status_transition(
    current_status: Any,
    next_status: Any,
    *,
    allow_same: bool = True,
) -> str:
    current_norm = normalize_booking_status(current_status)
    next_norm = normalize_booking_status(next_status, default="")
    if next_norm not in BOOKING_ALLOWED_STATUSES:
        raise ValueError(f"Unknown booking status: {next_status}")
    if allow_same and current_norm == next_norm:
        return next_norm
    allowed = BOOKING_STATUS_TRANSITIONS.get(current_norm, set())
    if next_norm not in allowed:
        raise ValueError(f"Booking status transition is not allowed: {current_norm} -> {next_norm}")
    return next_norm


def ensure_abonement_status_transition(
    current_status: Any,
    next_status: Any,
    *,
    allow_same: bool = True,
) -> str:
    current_norm = normalize_abonement_status(current_status)
    next_norm = normalize_abonement_status(next_status, default="")
    if next_norm not in ABONEMENT_ALLOWED_STATUSES:
        raise ValueError(f"Unknown abonement status: {next_status}")
    if allow_same and current_norm == next_norm:
        return next_norm
    allowed = ABONEMENT_STATUS_TRANSITIONS.get(current_norm, set())
    if next_norm not in allowed:
        raise ValueError(f"Abonement status transition is not allowed: {current_norm} -> {next_norm}")
    return next_norm


def set_booking_status(
    booking,
    next_status: Any,
    *,
    actor_staff_id: int | None = None,
    actor_username: str | None = None,
    actor_name: str | None = None,
    changed_at: datetime | None = None,
    allow_same: bool = True,
) -> str:
    resolved_status = ensure_booking_status_transition(
        getattr(booking, "status", None),
        next_status,
        allow_same=allow_same,
    )
    now = changed_at or utcnow()
    booking.status = resolved_status
    if hasattr(booking, "status_updated_at"):
        booking.status_updated_at = now
    if hasattr(booking, "status_updated_by_id"):
        booking.status_updated_by_id = actor_staff_id
    if hasattr(booking, "status_updated_by_username"):
        booking.status_updated_by_username = actor_username
    if hasattr(booking, "status_updated_by_name"):
        booking.status_updated_by_name = actor_name
    return resolved_status


def set_abonement_status(
    abonement,
    next_status: Any,
    *,
    allow_same: bool = True,
) -> str:
    resolved_status = ensure_abonement_status_transition(
        getattr(abonement, "status", None),
        next_status,
        allow_same=allow_same,
    )
    abonement.status = resolved_status
    return resolved_status

