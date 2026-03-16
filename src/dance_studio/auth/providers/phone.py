from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from dance_studio.auth.services.common import get_or_create_identity
from dance_studio.core.config import SESSION_PEPPER
from dance_studio.db.models import PhoneVerificationCode


def _hash_code(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}:{SESSION_PEPPER}".encode("utf-8")).hexdigest()


class PhoneCodeAuthProvider:
    provider_name = "phone"

    def request_code(self, db, phone: str, purpose: str = "login") -> str:
        code = f"{secrets.randbelow(899999) + 100000}"
        rec = PhoneVerificationCode(
            phone=phone,
            code_hash=_hash_code(phone, code),
            purpose=purpose,
            expires_at=datetime.utcnow() + timedelta(minutes=10),
            delivery_channel="internal",
            delivery_target=phone,
        )
        db.add(rec)
        return code

    def verify_code(self, db, phone: str, code: str):
        code_hash = _hash_code(phone, code)
        rec = (
            db.query(PhoneVerificationCode)
            .filter(
                PhoneVerificationCode.phone == phone,
                PhoneVerificationCode.code_hash == code_hash,
                PhoneVerificationCode.consumed_at.is_(None),
            )
            .order_by(PhoneVerificationCode.created_at.desc())
            .first()
        )
        if not rec:
            return None, "invalid_code"
        if rec.expires_at < datetime.utcnow():
            return None, "code_expired"
        rec.consumed_at = datetime.utcnow()
        user = get_or_create_identity(
            db,
            provider=self.provider_name,
            provider_user_id=phone,
            username=None,
            payload_json=None,
            fallback_name=f"User {phone}",
        )
        user.primary_phone = phone
        user.phone = user.phone or phone
        user.phone_verified_at = datetime.utcnow()
        return user, None
