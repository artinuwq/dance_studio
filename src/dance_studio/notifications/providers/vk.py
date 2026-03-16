from __future__ import annotations


class VkNotificationProvider:
    channel_type = "vk"

    def send(self, target_ref: str, title: str, body: str, payload: dict | None = None) -> dict:
        if not target_ref:
            return {"ok": False, "error": "missing_target_ref"}
        return {"ok": False, "error": "vk_not_configured"}
