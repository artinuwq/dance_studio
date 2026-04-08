from __future__ import annotations

from dance_studio.core.config import BOT_TOKEN
from dance_studio.core.telegram_http import telegram_api_post


class TelegramNotificationProvider:
    channel_type = "telegram"

    def send(self, target_ref: str, title: str, body: str, payload: dict | None = None) -> dict:
        if not target_ref:
            return {"ok": False, "error": "missing_target_ref", "is_permanent": True}
        if not BOT_TOKEN:
            return {"ok": False, "error": "telegram_not_configured"}

        try:
            chat_id = int(str(target_ref).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_telegram_target", "is_permanent": True}

        payload = payload or {}
        text = str(body or "").strip() or str(title or "").strip()
        if not text:
            return {"ok": False, "error": "empty_message", "is_permanent": True}

        request_payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": str(payload.get("parse_mode") or "HTML"),
        }
        if payload.get("reply_markup") is not None:
            request_payload["reply_markup"] = payload.get("reply_markup")
        if payload.get("disable_web_page_preview") is not None:
            request_payload["disable_web_page_preview"] = bool(payload.get("disable_web_page_preview"))

        ok, data, error = telegram_api_post(
            "sendMessage",
            request_payload,
            timeout=15,
        )
        if not ok:
            description = str(error or "").strip()
            lowered = description.lower()
            return {
                "ok": False,
                "error": description or "telegram_send_failed",
                "is_permanent": any(
                    marker in lowered
                    for marker in (
                        "chat not found",
                        "forbidden",
                        "bot was blocked by the user",
                        "user is deactivated",
                    )
                ),
            }

        message_id = ((data or {}).get("result") or {}).get("message_id")
        provider_message_id = f"tg:{message_id}" if message_id is not None else None
        return {"ok": True, "provider_message_id": provider_message_id}
