import os
from pathlib import Path
from typing import Optional
import requests

from dance_studio.core.config import (
    BOT_TOKEN,
    TECH_LOGS_CHAT_ID,
    TECH_BACKUPS_TOPIC_ID,
    TECH_CRITICAL_TOPIC_ID,
)


def _env_file_path() -> Path:
    # .env лежит в корне проекта
    return Path(__file__).resolve().parents[3] / ".env"


def _upsert_env_value(key: str, value: int) -> None:
    env_path = _env_file_path()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        existing_key = line.split("=", 1)[0].strip()
        if existing_key == key:
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _api_post(method: str, payload: dict) -> Optional[dict]:
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=5)
        data = resp.json() if resp.content else None
        if resp.ok:
            return data
        return data
    except Exception:
        return None
    return None


def _ensure_forum_topic(name: str, current_id: Optional[int], env_key: str) -> Optional[int]:
    if current_id:
        return current_id
    if not TECH_LOGS_CHAT_ID:
        return None
    data = _api_post("createForumTopic", {"chat_id": TECH_LOGS_CHAT_ID, "name": name})
    if not data or not data.get("ok"):
        return None
    topic_id = data.get("result", {}).get("message_thread_id")
    if topic_id:
        _upsert_env_value(env_key, int(topic_id))
        return int(topic_id)
    return None


def _send_message(topic_id: Optional[int], text: str) -> Optional[dict]:
    if not TECH_LOGS_CHAT_ID or not topic_id:
        return None
    data = _api_post(
        "sendMessage",
        {
            "chat_id": TECH_LOGS_CHAT_ID,
            "message_thread_id": topic_id,
            "text": text,
        },
    )
    return data


def send_critical_sync(text: str) -> None:
    topic_id = _ensure_forum_topic("Критичные ошибки", TECH_CRITICAL_TOPIC_ID, "TECH_CRITICAL_TOPIC_ID")
    if not topic_id:
        return
    data = _send_message(topic_id, text)
    if data and not data.get("ok"):
        if "message thread not found" in str(data.get("description", "")).lower():
            topic_id = _ensure_forum_topic("Критичные ошибки", None, "TECH_CRITICAL_TOPIC_ID")
            if topic_id:
                _send_message(topic_id, text)


def send_backup_sync(text: str) -> None:
    topic_id = _ensure_forum_topic("Бэкапы", TECH_BACKUPS_TOPIC_ID, "TECH_BACKUPS_TOPIC_ID")
    if not topic_id:
        return
    data = _send_message(topic_id, text)
    if data and not data.get("ok"):
        if "message thread not found" in str(data.get("description", "")).lower():
            topic_id = _ensure_forum_topic("Бэкапы", None, "TECH_BACKUPS_TOPIC_ID")
            if topic_id:
                _send_message(topic_id, text)
