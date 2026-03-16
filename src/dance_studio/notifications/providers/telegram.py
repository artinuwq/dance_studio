from __future__ import annotations


class TelegramNotificationProvider:
    channel_type = "telegram"

    def send(self, target_ref: str, title: str, body: str, payload: dict | None = None) -> dict:
        if not target_ref:
            return {"ok": False, "error": "missing_target_ref"}
        return {"ok": True, "provider_message_id": f"tg:{target_ref}"}
