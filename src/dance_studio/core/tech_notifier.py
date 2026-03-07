from __future__ import annotations

import logging
from typing import Optional

import requests

from dance_studio.core.config import (
    BOT_TOKEN,
    TECH_BACKUPS_TOPIC_ID,
    TECH_CRITICAL_TOPIC_ID,
    TECH_LOGS_CHAT_ID,
    TECH_NOTIFICATIONS_TOPIC_ID,
)
from dance_studio.core.system_settings_service import get_setting_value, update_setting
from dance_studio.db import get_session

_logger = logging.getLogger(__name__)

TECH_LOGS_CHAT_ID_SETTING_KEY = "tech.logs_chat_id"
TECH_BACKUPS_TOPIC_ID_SETTING_KEY = "tech.backups_topic_id"
TECH_CRITICAL_TOPIC_ID_SETTING_KEY = "tech.critical_topic_id"
TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY = "tech.notifications_topic_id"

_LEGACY_KEY_MAP = {
    "TECH_LOGS_CHAT_ID": TECH_LOGS_CHAT_ID_SETTING_KEY,
    "TECH_BACKUPS_TOPIC_ID": TECH_BACKUPS_TOPIC_ID_SETTING_KEY,
    "TECH_CRITICAL_TOPIC_ID": TECH_CRITICAL_TOPIC_ID_SETTING_KEY,
    "TECH_NOTIFICATIONS_TOPIC_ID": TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY,
}


def _normalize_setting_key(setting_key: str) -> str:
    normalized = str(setting_key or "").strip()
    return _LEGACY_KEY_MAP.get(normalized, normalized)


def _to_int_or_none(value) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0:
        return None
    return parsed


def _resolve_int_setting(setting_key: str, env_fallback: Optional[int]) -> Optional[int]:
    normalized_key = _normalize_setting_key(setting_key)
    fallback = _to_int_or_none(env_fallback)

    db = get_session()
    try:
        configured = _to_int_or_none(get_setting_value(db, normalized_key))
        if configured is not None:
            return configured

        if fallback is None:
            return None

        update_setting(
            db,
            key=normalized_key,
            raw_value=fallback,
            changed_by_staff_id=None,
            reason="Seeded from .env fallback during runtime use",
            source="runtime_migration",
        )
        db.commit()
        return fallback
    except Exception:
        db.rollback()
        _logger.exception("Failed to resolve runtime setting %s", normalized_key)
        return fallback
    finally:
        db.close()


def _persist_int_setting(setting_key: str, value: Optional[int], reason: str) -> None:
    normalized_key = _normalize_setting_key(setting_key)
    normalized_value = _to_int_or_none(value)
    if normalized_value is None:
        return

    db = get_session()
    try:
        update_setting(
            db,
            key=normalized_key,
            raw_value=normalized_value,
            changed_by_staff_id=None,
            reason=reason,
            source="runtime_sync",
        )
        db.commit()
    except Exception:
        db.rollback()
        _logger.exception("Failed to persist runtime setting %s", normalized_key)
    finally:
        db.close()


def resolve_tech_logs_chat_id() -> Optional[int]:
    return _resolve_int_setting(TECH_LOGS_CHAT_ID_SETTING_KEY, TECH_LOGS_CHAT_ID)


def resolve_tech_backups_topic_id() -> Optional[int]:
    return _resolve_int_setting(TECH_BACKUPS_TOPIC_ID_SETTING_KEY, TECH_BACKUPS_TOPIC_ID)


def resolve_tech_critical_topic_id() -> Optional[int]:
    return _resolve_int_setting(TECH_CRITICAL_TOPIC_ID_SETTING_KEY, TECH_CRITICAL_TOPIC_ID)


def resolve_tech_notifications_topic_id() -> Optional[int]:
    return _resolve_int_setting(TECH_NOTIFICATIONS_TOPIC_ID_SETTING_KEY, TECH_NOTIFICATIONS_TOPIC_ID)


def _api_post(method: str, payload: dict) -> Optional[dict]:
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=5)
        data = resp.json() if resp.content else None
        return data
    except Exception:
        return None


def _ensure_forum_topic(name: str, current_id: Optional[int], setting_key: str) -> Optional[int]:
    topic_id = _to_int_or_none(current_id)
    if topic_id:
        return topic_id

    chat_id = resolve_tech_logs_chat_id()
    if not chat_id:
        return None

    data = _api_post("createForumTopic", {"chat_id": chat_id, "name": name})
    if not data or not data.get("ok"):
        return None

    created_topic_id = _to_int_or_none(data.get("result", {}).get("message_thread_id"))
    if created_topic_id:
        _persist_int_setting(
            setting_key,
            created_topic_id,
            reason=f"Auto-created forum topic '{name}'",
        )
    return created_topic_id


def _send_message(topic_id: Optional[int], text: str) -> Optional[dict]:
    chat_id = resolve_tech_logs_chat_id()
    message_thread_id = _to_int_or_none(topic_id)
    if not chat_id or not message_thread_id:
        return None
    return _api_post(
        "sendMessage",
        {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "text": text,
        },
    )


def send_critical_sync(text: str) -> None:
    topic_id = _ensure_forum_topic(
        "Критичные ошибки",
        resolve_tech_critical_topic_id(),
        TECH_CRITICAL_TOPIC_ID_SETTING_KEY,
    )
    if not topic_id:
        return

    data = _send_message(topic_id, text)
    if data and not data.get("ok"):
        if "message thread not found" in str(data.get("description", "")).lower():
            topic_id = _ensure_forum_topic("Критичные ошибки", None, TECH_CRITICAL_TOPIC_ID_SETTING_KEY)
            if topic_id:
                _send_message(topic_id, text)


def send_backup_sync(text: str) -> None:
    topic_id = _ensure_forum_topic(
        "Бэкапы",
        resolve_tech_backups_topic_id(),
        TECH_BACKUPS_TOPIC_ID_SETTING_KEY,
    )
    if not topic_id:
        return

    data = _send_message(topic_id, text)
    if data and not data.get("ok"):
        if "message thread not found" in str(data.get("description", "")).lower():
            topic_id = _ensure_forum_topic("Бэкапы", None, TECH_BACKUPS_TOPIC_ID_SETTING_KEY)
            if topic_id:
                _send_message(topic_id, text)
