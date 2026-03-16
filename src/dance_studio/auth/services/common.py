from __future__ import annotations

from datetime import datetime

from dance_studio.db.models import AuthIdentity, User


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
