import html
import logging
from typing import Any

from aiogram import Bot

from dance_studio.core.tech_notifier import (
    TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
    _ensure_forum_topic,
    resolve_tech_logs_chat_id,
    resolve_tech_notifications_topic_id,
)

_logger = logging.getLogger(__name__)


async def _send_to_tech_chat_async(
    bot: Bot, text: str, topic_id: int | None, tech_chat_id: int | None = None
) -> bool:
    """Send duplicated message to tech chat with robust fallbacks."""
    chat_id = tech_chat_id if tech_chat_id is not None else resolve_tech_logs_chat_id()
    if not bot or not chat_id:
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if topic_id:
        payload["message_thread_id"] = topic_id

    try:
        await bot.send_message(**payload)
        return True
    except Exception as exc:
        error_text = str(exc).lower()

    if topic_id and "message thread not found" in error_text:
        recreated_topic_id = _ensure_forum_topic(
            "Уведомления юзерам",
            None,
            TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
        )
        if recreated_topic_id:
            retry_payload = {
                "chat_id": chat_id,
                "message_thread_id": recreated_topic_id,
                "text": text,
                "parse_mode": "HTML",
            }
            try:
                await bot.send_message(**retry_payload)
                return True
            except Exception:
                pass

    if topic_id:
        # Fallback for non-forum chats or broken thread id.
        fallback_payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            await bot.send_message(**fallback_payload)
            return True
        except Exception:
            return False

    return False


async def send_user_notification_async(
    bot: Bot,
    user_id: int,
    text: str,
    context_note: str = "Уведомление пользователю",
    parse_mode: str = "HTML",
    reply_markup: Any = None,
) -> bool:
    """Async send message to user and duplicate it to tech/admin logs chat."""
    if not bot:
        _logger.error("Bot instance not provided")
        return False

    user_ok = False
    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        user_ok = True
    except Exception as exc:
        _logger.error("Failed to send message to user %s: %s", user_id, exc)

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
            await _send_to_tech_chat_async(bot, info_text, topic_id, tech_chat_id)

            safe_text = html.escape(str(text or ""))
            quoted_text = f"<blockquote>{safe_text}</blockquote>" if safe_text else "<blockquote>—</blockquote>"
            await _send_to_tech_chat_async(bot, quoted_text, topic_id, tech_chat_id)
    except Exception:
        _logger.exception("Failed to duplicate notification to tech group")

    return user_ok
