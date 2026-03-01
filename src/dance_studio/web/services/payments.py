from __future__ import annotations

from dance_studio.db.models import PaymentProfile

PAYMENT_PROFILE_SLOTS = (1, 2)

def _ensure_payment_profiles(db):
    profiles = (
        db.query(PaymentProfile)
        .filter(PaymentProfile.slot.in_(PAYMENT_PROFILE_SLOTS))
        .order_by(PaymentProfile.slot.asc())
        .all()
    )
    by_slot = {int(p.slot): p for p in profiles}
    created = False

    for slot in PAYMENT_PROFILE_SLOTS:
        if slot not in by_slot:
            profile = PaymentProfile(
                slot=slot,
                title="Основные реквизиты" if slot == 1 else "Резервные реквизиты",
                details="",
                recipient_bank="",
                recipient_number="",
                recipient_full_name="",
                is_active=(slot == 1),
            )
            db.add(profile)
            by_slot[slot] = profile
            created = True

    if created:
        db.flush()

    active_profiles = [p for p in by_slot.values() if p.is_active]
    if not active_profiles:
        by_slot[1].is_active = True
    elif len(active_profiles) > 1:
        for p in active_profiles:
            p.is_active = (p.slot == 1)

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
    active = (
        db.query(PaymentProfile)
        .filter(PaymentProfile.slot.in_(PAYMENT_PROFILE_SLOTS), PaymentProfile.is_active.is_(True))
        .order_by(PaymentProfile.slot.asc())
        .first()
    )
    if not active:
        active = (
            db.query(PaymentProfile)
            .filter(PaymentProfile.slot.in_(PAYMENT_PROFILE_SLOTS))
            .order_by(PaymentProfile.slot.asc())
            .first()
        )
    if not active:
        return None
    payload = _serialize_payment_profile(active)
    payload["label"] = "Профиль 1" if active.slot == 1 else "Профиль 2"
    return payload

__all__ = [
    "PAYMENT_PROFILE_SLOTS",
    "_ensure_payment_profiles",
    "_get_active_payment_profile_payload",
    "_serialize_payment_profile",
]
