from __future__ import annotations

from dance_studio.auth.services.contracts import auth_error_payload
from dance_studio.db.models import AuthIdentity, User, UserPhone


class VerifiedPhoneRequiredError(RuntimeError):
    def __init__(self):
        super().__init__("phone_verification_required")


def user_has_verified_phone(db, *, user: User | None = None, user_id: int | None = None) -> bool:
    resolved_user_id = user_id or getattr(user, "id", None)
    if not resolved_user_id:
        return False
    if getattr(user, "phone_verified_at", None):
        return True
    verified_phone_exists = (
        db.query(UserPhone.id)
        .filter(UserPhone.user_id == int(resolved_user_id), UserPhone.verified_at.isnot(None))
        .first()
        is not None
    )
    if verified_phone_exists:
        return True
    return (
        db.query(AuthIdentity.id)
        .filter(
            AuthIdentity.user_id == int(resolved_user_id),
            AuthIdentity.provider == "phone",
            AuthIdentity.is_verified.is_(True),
        )
        .first()
        is not None
    )


def require_verified_phone(db, *, user: User | None = None, user_id: int | None = None) -> None:
    if user_has_verified_phone(db, user=user, user_id=user_id):
        return
    raise VerifiedPhoneRequiredError()


def verified_phone_required_payload() -> dict:
    return auth_error_payload(
        "phone_verification_required",
        message="Ваш аккаунт не подтверждён",
        action="verify_phone",
    )
