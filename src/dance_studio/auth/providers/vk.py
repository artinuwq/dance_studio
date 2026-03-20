from __future__ import annotations

import hashlib
import json

from dance_studio.auth.services.common import get_or_create_identity, normalize_phone_e164
from dance_studio.core.config import APP_SECRET_KEY


class VkMiniAppAuthProvider:
    provider_name = "vk"

    def _verify_signature(self, payload: dict) -> bool:
        sign = str(payload.get("sign") or "").strip().lower()
        if not sign:
            return False
        parts: list[str] = []
        for key in sorted(str(k) for k in payload.keys() if str(k) != "sign"):
            value = payload.get(key)
            if value is None:
                continue
            parts.append(f"{key}={value}")
        base = "&".join(parts) + APP_SECRET_KEY
        digest = hashlib.md5(base.encode("utf-8")).hexdigest()
        return digest == sign

    def authenticate(self, db, payload: dict, *, current_user_id: int | None = None):
        if not self._verify_signature(payload):
            return None, "invalid_vk_signature"
        vk_user_id = payload.get("vk_user_id") or payload.get("user_id")
        if not vk_user_id:
            return None, "vk_user_id_required"
        username = payload.get("vk_username") or payload.get("screen_name")
        verified_phone = None
        phone = payload.get("phone") or payload.get("phone_number")
        if payload.get("phone_verified") or payload.get("is_phone_verified"):
            verified_phone = normalize_phone_e164(str(phone or ""))
        user = get_or_create_identity(
            db,
            provider=self.provider_name,
            provider_user_id=str(vk_user_id),
            username=username,
            payload_json=json.dumps(payload, ensure_ascii=False),
            fallback_name=payload.get("name") or f"VK {vk_user_id}",
            verified_phone=verified_phone,
            current_user_id=current_user_id,
        )
        return user, None
