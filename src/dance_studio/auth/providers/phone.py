from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from dance_studio.auth.services.common import get_or_create_identity, normalize_phone_e164
from dance_studio.core.config import SESSION_PEPPER
from dance_studio.core.time import utcnow
from dance_studio.db.models import PhoneVerificationCode


OTP_RATE_LIMIT_SECONDS = 60


def _hash_code(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}:{SESSION_PEPPER}".encode("utf-8")).hexdigest()


class PhoneCodeAuthProvider:
    provider_name = "phone"

    def request_code(self, db, phone: str, purpose: str = "login") -> str:
        normalized_phone = normalize_phone_e164(phone)
        if not normalized_phone:
            raise ValueError("invalid_phone")
        latest = (
            db.query(PhoneVerificationCode)
            .filter(
                PhoneVerificationCode.phone == normalized_phone,
                PhoneVerificationCode.purpose == purpose,
            )
            .order_by(PhoneVerificationCode.created_at.desc())
            .first()
        )
        if latest and (utcnow() - latest.created_at).total_seconds() < OTP_RATE_LIMIT_SECONDS:
            raise ValueError("rate_limited")
        code = f"{secrets.randbelow(899999) + 100000}"
        rec = PhoneVerificationCode(
            phone=normalized_phone,
            code_hash=_hash_code(normalized_phone, code),
            purpose=purpose,
            expires_at=utcnow() + timedelta(minutes=10),
            delivery_channel="internal",
            delivery_target=normalized_phone,
        )
        db.add(rec)
        return code

    def set_delivery_metadata(
        self,
        db,
        *,
        phone: str,
        code: str,
        purpose: str = "login",
        delivery_channel: str,
        delivery_target: str | None = None,
    ) -> None:
        normalized_phone = normalize_phone_e164(phone)
        if not normalized_phone:
            return
        code_hash = _hash_code(normalized_phone, code)
        rec = (
            db.query(PhoneVerificationCode)
            .filter(
                PhoneVerificationCode.phone == normalized_phone,
                PhoneVerificationCode.purpose == purpose,
                PhoneVerificationCode.code_hash == code_hash,
            )
            .order_by(PhoneVerificationCode.created_at.desc())
            .first()
        )
        if not rec:
            return
        rec.delivery_channel = str(delivery_channel or "").strip() or rec.delivery_channel
        rec.delivery_target = str(delivery_target).strip() if delivery_target is not None else rec.delivery_target

    def verify_code(self, db, phone: str, code: str, *, current_user_id: int | None = None):
        normalized_phone = normalize_phone_e164(phone)
        if not normalized_phone:
            return None, "invalid_phone"
        code_hash = _hash_code(normalized_phone, code)
        rec = (
            db.query(PhoneVerificationCode)
            .filter(
                PhoneVerificationCode.phone == normalized_phone,
                PhoneVerificationCode.code_hash == code_hash,
                PhoneVerificationCode.consumed_at.is_(None),
            )
            .order_by(PhoneVerificationCode.created_at.desc())
            .first()
        )
        if not rec:
            return None, "invalid_code"
        if rec.expires_at < utcnow():
            return None, "code_expired"
        rec.consumed_at = utcnow()
        user = get_or_create_identity(
            db,
            provider=self.provider_name,
            provider_user_id=normalized_phone,
            username=None,
            payload_json=None,
            fallback_name=f"User {normalized_phone}",
            verified_phone=normalized_phone,
            current_user_id=current_user_id,
        )
        return user, None
