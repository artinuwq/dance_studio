from __future__ import annotations

import html
import json
import logging
from typing import Any

from dance_studio.auth.services.common import resolve_telegram_id_by_user, resolve_user_by_telegram
from dance_studio.core.config import BOT_TOKEN
from dance_studio.core.tech_notifier import (
    TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
    _ensure_forum_topic,
    resolve_tech_logs_chat_id,
    resolve_tech_notifications_topic_id,
)
from dance_studio.core.telegram_http import telegram_api_post
from dance_studio.db import get_session
from dance_studio.db.models import User
from dance_studio.notifications.providers.telegram import TelegramNotificationProvider
from dance_studio.notifications.services.notification_service import NotificationService

_logger = logging.getLogger(__name__)
_telegram_provider = TelegramNotificationProvider()


def _post_telegram_sync(payload: dict, timeout: int = 5) -> tuple[bool, str]:
    """Send Telegram Bot API request and return (ok, description)."""
    if not BOT_TOKEN:
        return False, "BOT_TOKEN not set"
    ok, _, error = telegram_api_post("sendMessage", payload, timeout=timeout)
    if ok:
        return True, ""
    return False, str(error or "")


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
            "User notifications",
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


def _safe_delivery_payload(parse_mode: str, reply_markup: Any = None) -> dict:
    payload: dict[str, Any] = {"parse_mode": parse_mode or "HTML"}
    if reply_markup is None:
        return payload
    try:
        json.dumps(reply_markup)
        payload["reply_markup"] = reply_markup
    except Exception:
        # Some markup objects are not JSON serializable.
        pass
    return payload


def _resolve_user_for_notification(db, user_or_telegram_id: int) -> User | None:
    user = resolve_user_by_telegram(db, user_or_telegram_id)
    if user:
        return user
    try:
        candidate_user_id = int(user_or_telegram_id)
    except (TypeError, ValueError):
        return None
    return db.query(User).filter(User.id == candidate_user_id, User.is_archived.is_(False)).first()


def _send_direct_telegram(target_ref: int | str, text: str, payload: dict) -> tuple[bool, str | None]:
    result = _telegram_provider.send(
        str(target_ref),
        "",
        text,
        payload=payload,
    )
    return bool(result.get("ok")), str(result.get("error") or "").strip() or None


def _iter_direct_telegram_targets(db, original_user_id: int, resolved_user: User | None) -> list[int]:
    candidates: list[int] = []
    original_resolved_via_telegram = resolve_user_by_telegram(db, original_user_id)
    if resolved_user:
        resolved_telegram_id = resolve_telegram_id_by_user(db, resolved_user.id)
        if resolved_telegram_id:
            candidates.append(int(resolved_telegram_id))
    try:
        fallback_target = int(original_user_id)
    except (TypeError, ValueError):
        fallback_target = 0
    original_target_is_explicit_telegram = bool(
        fallback_target
        and original_resolved_via_telegram
        and resolved_user
        and int(original_resolved_via_telegram.id) == int(resolved_user.id)
    )
    if fallback_target and (resolved_user is None or original_target_is_explicit_telegram):
        candidates.append(fallback_target)

    unique_targets: list[int] = []
    seen: set[int] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_targets.append(candidate)
    return unique_targets


def _send_via_multichannel(
    db,
    *,
    user: User,
    text: str,
    context_note: str,
    payload: dict,
) -> bool:
    service = NotificationService()
    notification = service.send(
        db,
        user_id=int(user.id),
        event_type="legacy_user_notification",
        title=context_note,
        body=text,
        payload=payload,
    )
    return notification.status == "sent"


def send_user_notification_sync(
    user_id: int,
    text: str,
    context_note: str = "User notification",
    parse_mode: str = "HTML",
    reply_markup: Any = None,
) -> bool:
    """Compatibility wrapper: route old calls through new multi-channel NotificationService."""
    delivery_payload = _safe_delivery_payload(parse_mode=parse_mode, reply_markup=reply_markup)
    user_ok = False
    delivery_error: str | None = None

    db = get_session()
    try:
        resolved_user = _resolve_user_for_notification(db, user_id)
        if resolved_user:
            user_ok = _send_via_multichannel(
                db,
                user=resolved_user,
                text=text,
                context_note=context_note,
                payload=delivery_payload,
            )
            # Legacy behavior expected direct Telegram by target chat id. If the
            # routed send fails, try the explicit target as a final fallback.
            if not user_ok:
                for direct_target in _iter_direct_telegram_targets(db, user_id, resolved_user):
                    user_ok, delivery_error = _send_direct_telegram(direct_target, text, delivery_payload)
                    if user_ok:
                        break
            db.commit()
        else:
            user_ok, delivery_error = _send_direct_telegram(user_id, text, delivery_payload)
            db.commit()
    except Exception:
        db.rollback()
        _logger.exception("Error sending user notification via notification service bridge")
        try:
            user_ok, delivery_error = _send_direct_telegram(user_id, text, delivery_payload)
        except Exception:
            user_ok = False
            delivery_error = "bridge_and_direct_failed"
    finally:
        db.close()

    try:
        topic_id = _ensure_forum_topic(
            "User notifications",
            resolve_tech_notifications_topic_id(),
            TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
        )

        tech_chat_id = resolve_tech_logs_chat_id()
        if tech_chat_id:
            safe_context = html.escape(str(context_note or "User notification"))
            status_text = "sent" if user_ok else "failed"
            info_text = (
                f"<b>Notification</b>\n"
                f"Context: <b>{safe_context}</b>\n"
                f"Target: <code>{user_id}</code>\n"
                f"Status: <b>{status_text}</b>"
            )
            if delivery_error:
                info_text += f"\nError: <code>{html.escape(delivery_error)}</code>"
            _send_to_tech_chat_sync(info_text, topic_id, tech_chat_id)

            safe_text = html.escape(str(text or ""))
            quoted_text = f"<blockquote>{safe_text or '—'}</blockquote>"
            _send_to_tech_chat_sync(quoted_text, topic_id, tech_chat_id)
    except Exception:
        _logger.exception("Failed to duplicate notification to tech group")

    return user_ok
