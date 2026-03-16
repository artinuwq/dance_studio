from __future__ import annotations


class WebPushNotificationProvider:
    channel_type = "web_push"

    def send(self, target_ref: str, title: str, body: str, payload: dict | None = None) -> dict:
        if not target_ref:
            return {"ok": False, "error": "missing_target_ref"}
        return {"ok": False, "error": "web_push_not_configured"}
