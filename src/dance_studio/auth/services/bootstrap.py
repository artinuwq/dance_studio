from __future__ import annotations

from dance_studio.db.models import AuthIdentity, PasskeyCredential, User, UserPhone
from dance_studio.auth.services.verification import user_has_verified_phone


AVAILABLE_AUTH_METHODS = ["telegram", "vk", "phone", "passkey"]


def build_user_auth_contract(db, user: User | None) -> dict | None:
    if not user:
        return None
    identities = db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).all()
    passkeys = db.query(PasskeyCredential).filter(PasskeyCredential.user_id == user.id).all()
    phones = db.query(UserPhone).filter(UserPhone.user_id == user.id).all()
    phone_verified = user_has_verified_phone(db, user=user)
    auth_methods = sorted({identity.provider for identity in identities if identity.provider} | ({"passkey"} if passkeys else set()))
    return {
        "id": user.id,
        "name": user.name,
        "phone_verified": phone_verified,
        "requires_manual_merge": bool(user.requires_manual_merge),
        "auth_methods": auth_methods,
        "identities": {
            "telegram": {"linked": any(identity.provider == "telegram" for identity in identities)},
            "vk": {"linked": any(identity.provider == "vk" for identity in identities)},
            "phone": {"linked": any(identity.provider == "phone" for identity in identities), "verified": phone_verified},
            "passkey": {
                "linked": bool(passkeys),
                "count": len(passkeys),
                "items": [
                    {
                        "credential_id": row.credential_id,
                        "device_name": row.device_name,
                        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    for row in passkeys
                ],
            },
        },
        "deprecated": {
            "legacy_user_fields": {
                "telegram_id": user.telegram_id,
                "primary_phone": user.primary_phone,
                "preferred_notification_channel": user.preferred_notification_channel,
            }
        },
    }


def auth_feature_flags() -> dict:
    return {"passkey_scaffold": False, "passkey_webauthn": True}
