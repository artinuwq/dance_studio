from __future__ import annotations

from datetime import datetime

from dance_studio.db.models import AuthIdentity, User


def resolve_user_id_by_telegram(db, telegram_id: int | str | None) -> int | None:
    if telegram_id is None:
        return None
    try:
        telegram_id_str = str(int(telegram_id))
    except (TypeError, ValueError):
        return None

    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == "telegram",
            AuthIdentity.provider_user_id == telegram_id_str,
        )
        .first()
    )
    if not identity:
        return None
    return identity.user_id


def resolve_user_by_telegram(db, telegram_id: int | str | None, *, allow_legacy: bool = True) -> User | None:
    if telegram_id is None:
        return None
    resolved_user_id = resolve_user_id_by_telegram(db, telegram_id)
    if resolved_user_id:
        return db.query(User).filter_by(id=resolved_user_id).first()

    if not allow_legacy:
        return None

    try:
        telegram_id_int = int(telegram_id)
    except (TypeError, ValueError):
        return None

    user = db.query(User).filter_by(telegram_id=telegram_id_int).first()
    if not user:
        return None

    existing_identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == "telegram",
            AuthIdentity.provider_user_id == str(telegram_id_int),
        )
        .first()
    )
    if not existing_identity:
        db.add(
            AuthIdentity(
                user_id=user.id,
                provider="telegram",
                provider_user_id=str(telegram_id_int),
                provider_username=user.username,
                payload_json=None,
                is_primary=True,
                is_verified=True,
            )
        )
    return user


def get_or_create_identity(db, *, provider: str, provider_user_id: str | None, username: str | None, payload_json: str | None, fallback_name: str) -> User:
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
            return user

    user = User(name=fallback_name)
    user.last_login_at = datetime.utcnow()
    db.add(user)
    db.flush()

    db.add(
        AuthIdentity(
            user_id=user.id,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_username=username,
            provider_payload_json=payload_json,
            is_primary=True,
            is_verified=True,
        )
    )
    return user
