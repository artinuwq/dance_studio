from __future__ import annotations

from datetime import datetime

from dance_studio.db.models import AuthIdentity, User, UserPhone


PROVIDERS_WITH_PHONE_MERGE = {"telegram", "vk", "phone"}


def normalize_phone_e164(phone: str | None) -> str | None:
    raw = (phone or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    if raw.startswith("+"):
        normalized = f"+{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        normalized = f"+7{digits[1:]}"
    elif len(digits) == 10:
        normalized = f"+7{digits}"
    else:
        normalized = f"+{digits}"
    if len(normalized) < 8:
        return None
    return normalized


def get_verified_phone_user(db, *, phone_e164: str | None, exclude_user_id: int | None = None) -> tuple[User | None, list[int]]:
    if not phone_e164:
        return None, []
    query = db.query(UserPhone).filter(
        UserPhone.phone_e164 == phone_e164,
        UserPhone.verified_at.isnot(None),
    )
    if exclude_user_id is not None:
        query = query.filter(UserPhone.user_id != exclude_user_id)
    rows = query.all()
    user_ids = sorted({row.user_id for row in rows})
    if len(user_ids) != 1:
        return None, user_ids
    user = db.query(User).filter(User.id == user_ids[0]).first()
    return user, user_ids


def ensure_user_phone(
    db,
    *,
    user_id: int,
    phone_e164: str | None,
    source: str,
    verified_at: datetime | None,
    is_primary: bool = False,
) -> UserPhone | None:
    if not phone_e164:
        return None
    phone = db.query(UserPhone).filter(UserPhone.phone_e164 == phone_e164).first()
    if phone and phone.user_id != user_id:
        return phone
    if not phone:
        phone = UserPhone(
            user_id=user_id,
            phone_e164=phone_e164,
            source=source,
            verified_at=verified_at,
            is_primary=is_primary,
        )
        db.add(phone)
        db.flush()
    else:
        phone.source = source or phone.source
        phone.verified_at = verified_at or phone.verified_at
        phone.is_primary = phone.is_primary or is_primary
    user = db.query(User).filter(User.id == user_id).first()
    if user and verified_at:
        user.primary_phone = phone_e164
        user.phone = user.phone or phone_e164
        user.phone_verified_at = verified_at
    return phone


def _link_identity_to_user(
    db,
    *,
    user: User,
    provider: str,
    provider_user_id: str | None,
    username: str | None,
    payload_json: str | None,
    verified: bool,
) -> AuthIdentity:
    identity = None
    if provider_user_id:
        identity = (
            db.query(AuthIdentity)
            .filter(AuthIdentity.provider == provider, AuthIdentity.provider_user_id == provider_user_id)
            .first()
        )
    now = datetime.utcnow()
    if identity:
        identity.user_id = user.id
        identity.provider_username = username
        identity.provider_payload_json = payload_json
        identity.is_verified = verified or identity.is_verified
        identity.last_login_at = now
        return identity

    identity = AuthIdentity(
        user_id=user.id,
        provider=provider,
        provider_user_id=provider_user_id,
        provider_username=username,
        provider_payload_json=payload_json,
        linked_at=now,
        last_login_at=now,
        is_primary=False,
        is_verified=verified,
    )
    db.add(identity)
    return identity


def get_or_create_identity(
    db,
    *,
    provider: str,
    provider_user_id: str | None,
    username: str | None,
    payload_json: str | None,
    fallback_name: str,
    verified_phone: str | None = None,
    current_user_id: int | None = None,
    is_verified: bool = True,
) -> User:
    identity = None
    if provider_user_id:
        identity = (
            db.query(AuthIdentity)
            .filter(AuthIdentity.provider == provider, AuthIdentity.provider_user_id == provider_user_id)
            .first()
        )
    if identity:
        user = db.query(User).filter(User.id == identity.user_id).first()
        if user:
            user.last_login_at = datetime.utcnow()
            identity.provider_username = username
            identity.provider_payload_json = payload_json
            identity.last_login_at = user.last_login_at
            if verified_phone:
                ensure_user_phone(
                    db,
                    user_id=user.id,
                    phone_e164=verified_phone,
                    source=provider,
                    verified_at=datetime.utcnow(),
                    is_primary=user.primary_phone in (None, verified_phone),
                )
            return user

    target_user = None
    if current_user_id:
        target_user = db.query(User).filter(User.id == current_user_id).first()

    matched_ids: list[int] = []
    if target_user is None and provider in PROVIDERS_WITH_PHONE_MERGE and verified_phone:
        target_user, matched_ids = get_verified_phone_user(db, phone_e164=verified_phone)

    if target_user is None:
        target_user = User(name=fallback_name)
        db.add(target_user)
        db.flush()

    target_user.last_login_at = datetime.utcnow()
    _link_identity_to_user(
        db,
        user=target_user,
        provider=provider,
        provider_user_id=provider_user_id,
        username=username,
        payload_json=payload_json,
        verified=is_verified,
    )

    if verified_phone:
        ensure_user_phone(
            db,
            user_id=target_user.id,
            phone_e164=verified_phone,
            source=provider,
            verified_at=datetime.utcnow(),
            is_primary=target_user.primary_phone in (None, verified_phone),
        )

    target_user._phone_match_user_ids = matched_ids  # type: ignore[attr-defined]
    return target_user
