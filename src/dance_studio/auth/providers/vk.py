from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

from dance_studio.auth.services.common import get_or_create_identity, normalize_phone_e164
from dance_studio.core.config import APP_SECRET_KEY, VK_MINI_APP_SECRET_KEY, VK_MINI_APP_SERVICE_KEY


class VkMiniAppAuthProvider:
    provider_name = "vk"
    _NON_LAUNCH_VK_KEYS = {"vk_username"}

    @staticmethod
    def _signature_secrets() -> list[str]:
        values: list[str] = []
        for candidate in (VK_MINI_APP_SECRET_KEY, VK_MINI_APP_SERVICE_KEY, APP_SECRET_KEY):
            normalized = str(candidate or "").strip()
            if normalized and normalized not in values:
                values.append(normalized)
        return values

    @staticmethod
    def _collect_vk_params(payload: dict) -> dict[str, str]:
        params: dict[str, str] = {}
        for raw_key, raw_value in payload.items():
            key = str(raw_key)
            if not key.startswith("vk_"):
                continue
            if key in VkMiniAppAuthProvider._NON_LAUNCH_VK_KEYS:
                continue
            if raw_value is None:
                continue
            params[key] = str(raw_value)
        return params

    @staticmethod
    def _hmac_sha256_base64url(data: str, secret: str) -> str:
        digest = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _verify_signature(self, payload: dict) -> bool:
        sign = str(payload.get("sign") or "").strip()
        if not sign:
            return False
        vk_params = self._collect_vk_params(payload)
        if not vk_params:
            return False
        params_string = urlencode(sorted(vk_params.items()), doseq=True)
        sign_lower = sign.lower()
        for secret in self._signature_secrets():
            expected = self._hmac_sha256_base64url(params_string, secret)
            if hmac.compare_digest(expected, sign):
                return True

            # Backward-compatibility for legacy md5-based signatures.
            legacy_md5 = hashlib.md5(f"{params_string}{secret}".encode("utf-8")).hexdigest()
            if hmac.compare_digest(legacy_md5, sign_lower):
                return True
        return False

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
