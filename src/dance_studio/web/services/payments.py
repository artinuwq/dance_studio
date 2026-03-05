from __future__ import annotations

from datetime import date, time

from dance_studio.db.models import BookingRequest, Direction, Group, PaymentProfile
from dance_studio.web.services.studio_rules import (
    PRIMARY_OWNER_KEY,
    SECONDARY_OWNER_KEY,
    owner_for_group_direction,
    owner_for_interval,
)

PAYMENT_PROFILE_PRIMARY_SLOT = 1
PAYMENT_PROFILE_SECONDARY_SLOTS = (2, 3)
PAYMENT_PROFILE_SLOTS = (
    PAYMENT_PROFILE_PRIMARY_SLOT,
    PAYMENT_PROFILE_SECONDARY_SLOTS[0],
    PAYMENT_PROFILE_SECONDARY_SLOTS[1],
)

PAYMENT_PROFILE_DEFAULT_TITLES = {
    PAYMENT_PROFILE_PRIMARY_SLOT: "Реквизиты 1",
    PAYMENT_PROFILE_SECONDARY_SLOTS[0]: "Реквизиты 2",
    PAYMENT_PROFILE_SECONDARY_SLOTS[1]: "Реквизиты 3",
}


def _default_payment_profile_title(slot: int) -> str:
    normalized_slot = int(slot)
    return PAYMENT_PROFILE_DEFAULT_TITLES.get(normalized_slot) or f"Реквизиты {normalized_slot}"


def _ensure_payment_profiles(db):
    profiles = (
        db.query(PaymentProfile)
        .filter(PaymentProfile.slot.in_(PAYMENT_PROFILE_SLOTS))
        .order_by(PaymentProfile.slot.asc())
        .all()
    )
    by_slot = {int(p.slot): p for p in profiles}
    changed = False

    for slot in PAYMENT_PROFILE_SLOTS:
        expected_title = _default_payment_profile_title(slot)
        if slot not in by_slot:
            profile = PaymentProfile(
                slot=slot,
                title=expected_title,
                details="",
                recipient_bank="",
                recipient_number="",
                recipient_full_name="",
                is_active=(slot == PAYMENT_PROFILE_SECONDARY_SLOTS[0]),
            )
            db.add(profile)
            by_slot[slot] = profile
            changed = True
            continue

        profile = by_slot[slot]
        if (profile.title or "").strip() != expected_title:
            profile.title = expected_title
            changed = True

    if changed:
        db.flush()

    # is_active is used for owner-2 switch (slot 2 / slot 3) only.
    if PAYMENT_PROFILE_PRIMARY_SLOT in by_slot:
        by_slot[PAYMENT_PROFILE_PRIMARY_SLOT].is_active = False

    active_secondary = [
        by_slot[slot]
        for slot in PAYMENT_PROFILE_SECONDARY_SLOTS
        if slot in by_slot and by_slot[slot].is_active
    ]
    if not active_secondary:
        default_slot = PAYMENT_PROFILE_SECONDARY_SLOTS[0]
        if default_slot in by_slot:
            by_slot[default_slot].is_active = True
    elif len(active_secondary) > 1:
        keep_slot = PAYMENT_PROFILE_SECONDARY_SLOTS[0]
        for slot in PAYMENT_PROFILE_SECONDARY_SLOTS:
            if slot in by_slot:
                by_slot[slot].is_active = (slot == keep_slot)

    return by_slot


def _serialize_payment_profile(profile: PaymentProfile) -> dict:
    recipient_bank = (profile.recipient_bank or "").strip()
    recipient_number = (profile.recipient_number or "").strip()
    recipient_full_name = (profile.recipient_full_name or "").strip()
    details = (
        f"Банк получателя: {recipient_bank or '—'}\n"
        f"Номер: {recipient_number or '—'}\n"
        f"ФИО получателя: {recipient_full_name or '—'}"
    )
    return {
        "slot": int(profile.slot),
        "title": profile.title or "",
        "details": details,
        "recipient_bank": recipient_bank,
        "recipient_number": recipient_number,
        "recipient_full_name": recipient_full_name,
        "is_active": bool(profile.is_active),
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def _get_active_payment_profile_payload(db) -> dict | None:
    profiles = _ensure_payment_profiles(db)
    active = next(
        (
            profiles[slot]
            for slot in PAYMENT_PROFILE_SECONDARY_SLOTS
            if slot in profiles and profiles[slot].is_active
        ),
        None,
    )
    if not active:
        active = next((profiles.get(slot) for slot in PAYMENT_PROFILE_SECONDARY_SLOTS if profiles.get(slot)), None)
    if not active:
        active = profiles.get(PAYMENT_PROFILE_PRIMARY_SLOT)
    if not active:
        return None
    payload = _serialize_payment_profile(active)
    payload["label"] = f"Реквизиты {int(active.slot)}"
    return payload


def _get_secondary_owner_active_slot(db) -> int:
    profiles = _ensure_payment_profiles(db)
    for slot in PAYMENT_PROFILE_SECONDARY_SLOTS:
        row = profiles.get(slot)
        if row and row.is_active:
            return slot
    return PAYMENT_PROFILE_SECONDARY_SLOTS[0]


def _get_payment_profile_payload_for_slot(db, slot: int) -> dict | None:
    profiles = _ensure_payment_profiles(db)
    profile = profiles.get(int(slot))
    if not profile:
        return None
    payload = _serialize_payment_profile(profile)
    payload["label"] = f"Реквизиты {int(slot)}"
    return payload


def _select_payment_slot_for_context(
    *,
    object_type: str | None,
    booking_date: date | None = None,
    time_from: time | None = None,
    time_to: time | None = None,
    group_direction_type: str | None = None,
    secondary_owner_active_slot: int = PAYMENT_PROFILE_SECONDARY_SLOTS[0],
) -> int:
    normalized_type = str(object_type or "").strip().lower()
    normalized_secondary_slot = (
        secondary_owner_active_slot
        if secondary_owner_active_slot in PAYMENT_PROFILE_SECONDARY_SLOTS
        else PAYMENT_PROFILE_SECONDARY_SLOTS[0]
    )

    if normalized_type == "group":
        owner_key = owner_for_group_direction(group_direction_type)
        if owner_key == PRIMARY_OWNER_KEY:
            return PAYMENT_PROFILE_PRIMARY_SLOT
        if owner_key == SECONDARY_OWNER_KEY:
            return normalized_secondary_slot
        return normalized_secondary_slot

    if normalized_type in {"rental", "individual"}:
        owner_key = owner_for_interval(booking_date, time_from, time_to)
        if owner_key == SECONDARY_OWNER_KEY:
            return normalized_secondary_slot
        return PAYMENT_PROFILE_PRIMARY_SLOT

    return PAYMENT_PROFILE_PRIMARY_SLOT


def _resolve_payment_profile_payload(
    db,
    *,
    object_type: str | None,
    booking_date: date | None = None,
    time_from: time | None = None,
    time_to: time | None = None,
    group_direction_type: str | None = None,
) -> dict | None:
    secondary_slot = _get_secondary_owner_active_slot(db)
    slot = _select_payment_slot_for_context(
        object_type=object_type,
        booking_date=booking_date,
        time_from=time_from,
        time_to=time_to,
        group_direction_type=group_direction_type,
        secondary_owner_active_slot=secondary_slot,
    )
    return _get_payment_profile_payload_for_slot(db, slot)


def _resolve_payment_profile_payload_for_booking(
    db,
    booking: BookingRequest,
    *,
    group_direction_type: str | None = None,
) -> dict | None:
    normalized_type = str(getattr(booking, "object_type", "") or "").strip().lower()
    resolved_direction_type = group_direction_type

    if normalized_type == "group" and not resolved_direction_type:
        group_id = getattr(booking, "group_id", None)
        if group_id:
            row = (
                db.query(Direction.direction_type)
                .join(Group, Group.direction_id == Direction.direction_id)
                .filter(Group.id == int(group_id))
                .first()
            )
            if row:
                resolved_direction_type = row[0]

    return _resolve_payment_profile_payload(
        db,
        object_type=normalized_type,
        booking_date=getattr(booking, "date", None),
        time_from=getattr(booking, "time_from", None),
        time_to=getattr(booking, "time_to", None),
        group_direction_type=resolved_direction_type,
    )


__all__ = [
    "PAYMENT_PROFILE_PRIMARY_SLOT",
    "PAYMENT_PROFILE_SECONDARY_SLOTS",
    "PAYMENT_PROFILE_SLOTS",
    "_ensure_payment_profiles",
    "_get_active_payment_profile_payload",
    "_get_payment_profile_payload_for_slot",
    "_get_secondary_owner_active_slot",
    "_resolve_payment_profile_payload",
    "_resolve_payment_profile_payload_for_booking",
    "_select_payment_slot_for_context",
    "_serialize_payment_profile",
]
