from __future__ import annotations

from datetime import datetime

from dance_studio.db.models import PasskeyCredential


class PasskeyAuthProvider:
    provider_name = "passkey"

    def register_begin(self, user_id: int):
        return {
            "status": "ok",
            "user_id": user_id,
            "challenge": f"passkey-register-{user_id}",
            "rp": "dance-studio",
            "fallback_auth_methods": ["telegram", "vk", "phone"],
        }

    def register_complete(self, db, *, user_id: int, payload: dict):
        credential_id = str(payload.get("credential_id") or "").strip()
        public_key = str(payload.get("public_key") or "").strip()
        if not user_id:
            return None, "auth_required"
        if not credential_id or not public_key:
            return None, "credential_id_and_public_key_required"
        credential = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == credential_id).first()
        if credential:
            if credential.user_id != user_id:
                return None, "credential_already_registered"
            return None, "duplicate_passkey"
        credential = PasskeyCredential(
            user_id=user_id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=int(payload.get("sign_count") or 0),
            transports=",".join(payload.get("transports") or []),
            device_name=str(payload.get("device_name") or "Passkey").strip() or "Passkey",
            created_at=datetime.utcnow(),
        )
        db.add(credential)
        return credential, None

    def delete_credential(self, db, *, user_id: int, credential_id: str):
        credential = db.query(PasskeyCredential).filter(PasskeyCredential.user_id == user_id, PasskeyCredential.credential_id == credential_id).first()
        if not credential:
            return False
        db.delete(credential)
        return True

    def login_begin(self):
        return {"status": "ok", "challenge": "passkey-login", "fallback_auth_methods": ["telegram", "vk", "phone"]}

    def login_complete(self, db, payload: dict):
        credential_id = str(payload.get("credential_id") or "").strip()
        if not credential_id:
            return None, "credential_id_required"
        credential = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == credential_id).first()
        if not credential:
            return None, "credential_not_found"
        raw_sign_count = payload.get("sign_count")
        next_sign_count = credential.sign_count or 0 if raw_sign_count is None else int(raw_sign_count)
        if next_sign_count < (credential.sign_count or 0):
            return None, "invalid_sign_count"
        credential.sign_count = next_sign_count
        credential.last_used_at = datetime.utcnow()
        return credential, None
