from __future__ import annotations

import json

from dance_studio.auth.services.common import get_or_create_identity


class VkMiniAppAuthProvider:
    provider_name = "vk"

    def authenticate(self, db, payload: dict):
        vk_user_id = payload.get("vk_user_id") or payload.get("user_id")
        if not vk_user_id:
            return None, "vk_user_id_required"
        username = payload.get("vk_username") or payload.get("screen_name")
        user = get_or_create_identity(
            db,
            provider=self.provider_name,
            provider_user_id=str(vk_user_id),
            username=username,
            payload_json=json.dumps(payload, ensure_ascii=False),
            fallback_name=payload.get("name") or f"VK {vk_user_id}",
        )
        return user, None
