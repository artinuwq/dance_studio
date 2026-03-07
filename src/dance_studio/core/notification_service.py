import html
import logging
from typing import Any

import requests

from dance_studio.core.config import BOT_TOKEN
from dance_studio.core.tech_notifier import (
    TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
    _ensure_forum_topic,
    resolve_tech_logs_chat_id,
    resolve_tech_notifications_topic_id,
)

_logger = logging.getLogger(__name__)


def _post_telegram_sync(payload: dict, timeout: int = 5) -> tuple[bool, str]:
    """Send Telegram Bot API request and return (ok, description)."""
    if not BOT_TOKEN:
        return False, "BOT_TOKEN not set"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=timeout,
        )
        if resp.ok:
            return True, ""

        description = ""
        try:
            data = resp.json() if resp.content else {}
            description = str((data or {}).get("description", ""))
        except Exception:
            description = resp.text or ""
        return False, description
    except Exception as exc:
        return False, str(exc)


def _send_to_tech_chat_sync(text: str, topic_id: int | None, tech_chat_id: int | None = None) -> bool:
    """Send duplicated message to tech chat with robust fallbacks."""
    chat_id = tech_chat_id if tech_chat_id is not None else resolve_tech_logs_chat_id()
    if not chat_id:
        return False

    base_payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    payload = dict(base_payload)
    if topic_id:
        payload["message_thread_id"] = topic_id

    ok, description = _post_telegram_sync(payload)
    if ok:
        return True

    if topic_id and "message thread not found" in description.lower():
        recreated_topic_id = _ensure_forum_topic(
            "Уведомления юзерам",
            None,
            TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
        )
        if recreated_topic_id:
            retry_payload = dict(base_payload)
            retry_payload["message_thread_id"] = recreated_topic_id
            retry_ok, _ = _post_telegram_sync(retry_payload)
            if retry_ok:
                return True

    if topic_id:
        # Fallback for non-forum chats or broken thread id.
        fallback_ok, _ = _post_telegram_sync(base_payload)
        return fallback_ok

    return False


def send_user_notification_sync(
    user_id: int,
    text: str,
    context_note: str = "Уведомление пользователю",
    parse_mode: str = "HTML",
    reply_markup: Any = None,
) -> bool:
    """Send message to user and duplicate it to tech/admin logs chat."""
    if not BOT_TOKEN:
        _logger.error("BOT_TOKEN not set, cannot send notification")
        return False

    user_ok = False
    try:
        payload = {
            "chat_id": user_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        if resp.ok:
            user_ok = True
        else:
            _logger.error("Failed to send message to user %s: %s", user_id, resp.text)
    except Exception:
        _logger.exception("Error sending message to user %s", user_id)

    try:
        topic_id = _ensure_forum_topic(
            "Уведомления юзерам",
            resolve_tech_notifications_topic_id(),
            TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
        )

        tech_chat_id = resolve_tech_logs_chat_id()
        if tech_chat_id:
            safe_context = html.escape(str(context_note or "Уведомление пользователю"))
            info_text = (
                f"<b>🔔 {safe_context}</b>\n"
                f"👤 Кому: <code>{user_id}</code>\n"
                f"✅ Статус: {'Отправлено' if user_ok else '❌ ОШИБКА'}"
            )
            _send_to_tech_chat_sync(info_text, topic_id, tech_chat_id)

            safe_text = html.escape(str(text or ""))
            quoted_text = f"<blockquote>{safe_text}</blockquote>" if safe_text else "<blockquote>—</blockquote>"
            _send_to_tech_chat_sync(quoted_text, topic_id, tech_chat_id)
    except Exception:
        _logger.exception("Failed to duplicate notification to tech group")

    return user_ok
