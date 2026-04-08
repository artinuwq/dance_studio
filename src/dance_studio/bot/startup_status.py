from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from dance_studio.bot.telegram_userbot import API_HASH as USERBOT_API_HASH
from dance_studio.bot.telegram_userbot import API_ID as USERBOT_API_ID
from dance_studio.bot.telegram_userbot import SESSION_PATH as USERBOT_SESSION_PATH
from dance_studio.bot.telegram_userbot import USERBOT_CONNECTION_RETRIES
from dance_studio.bot.telegram_userbot import USERBOT_REQUEST_RETRIES
from dance_studio.bot.telegram_userbot import USERBOT_RETRY_DELAY_SECONDS
from dance_studio.bot.telegram_userbot import resolve_userbot_proxy_candidates
from dance_studio.core.config import (
    VK_COMMUNITY_ACCESS_TOKEN,
    VK_COMMUNITY_ID,
    VK_MINI_APP_APP_ID,
    VK_MINI_APP_SECRET_KEY,
    VK_MINI_APP_SERVICE_KEY,
)
from telethon import TelegramClient


def _parse_positive_int(raw_value) -> int | None:
    try:
        value = int(str(raw_value or "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _mask_path_name(raw_path: str | None) -> str:
    name = Path(str(raw_path or "").strip() or "userbot.session").name
    return name or "userbot.session"


def _status_line(label: str, state: str, details: str = "") -> str:
    suffix = f" ({details})" if details else ""
    return f"{label}: {state}{suffix}"


def describe_userbot_status(
    *,
    api_id: str | None = None,
    api_hash: str | None = None,
    session_path: str | None = None,
) -> str:
    resolved_api_id = str(USERBOT_API_ID if api_id is None else api_id).strip()
    resolved_api_hash = str(USERBOT_API_HASH if api_hash is None else api_hash).strip()
    resolved_session_path = USERBOT_SESSION_PATH if session_path is None else session_path

    if not resolved_api_id or not resolved_api_hash:
        return _status_line("User-bot", "Ð Ð…Ð Âµ Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…", "Ð Ð…Ð ÂµÐ¡â€š TELEGRAM_API_ID/TELEGRAM_API_HASH")

    session_file = Path(str(resolved_session_path or "").strip())
    if not str(session_file):
        return _status_line("User-bot", "Ð Ð…Ð Âµ Ð Ñ–Ð Ñ•Ð¡â€šÐ Ñ•Ð Ð†", "Ð Ñ—Ð¡Ñ“Ð¡â€šÐ¡ÐŠ Ð Ò‘Ð Ñ• session Ð Ð…Ð Âµ Ð Â·Ð Â°Ð Ò‘Ð Â°Ð Ð…")

    if not session_file.exists():
        return _status_line("User-bot", "Ð Ð…Ð¡Ñ“Ð Â¶Ð ÂµÐ Ð… Ð Â»Ð Ñ•Ð Ñ–Ð Ñ‘Ð Ð…", f"Ð Ð…Ð ÂµÐ¡â€š Ð¡â€žÐ Â°Ð â„–Ð Â»Ð Â° {_mask_path_name(str(session_file))}")

    if session_file.is_dir():
        return _status_line("User-bot", "Ð Ñ•Ð¡â‚¬Ð Ñ‘Ð Â±Ð Ñ”Ð Â°", f"{_mask_path_name(str(session_file))} Ð¡ÐŒÐ¡â€šÐ Ñ• Ð Ò‘Ð Ñ‘Ð¡Ð‚Ð ÂµÐ Ñ”Ð¡â€šÐ Ñ•Ð¡Ð‚Ð Ñ‘Ð¡Ð")

    if session_file.stat().st_size <= 0:
        return _status_line("User-bot", "Ð Ð…Ð¡Ñ“Ð Â¶Ð ÂµÐ Ð… Ð Â»Ð Ñ•Ð Ñ–Ð Ñ‘Ð Ð…", f"Ð Ñ—Ð¡Ñ“Ð¡ÐƒÐ¡â€šÐ Ñ•Ð â„– {_mask_path_name(str(session_file))}")

    return _status_line("User-bot", "Ð Ñ–Ð Ñ•Ð¡â€šÐ Ñ•Ð Ð†", f"session={_mask_path_name(str(session_file))}")


def _normalize_userbot_probe_error(exc: Exception) -> str:
    message = str(exc or "").strip().lower()
    if not message:
        return "Ð Ð…Ð ÂµÐ Ñ‘Ð Â·Ð Ð†Ð ÂµÐ¡ÐƒÐ¡â€šÐ Ð…Ð Â°Ð¡Ð Ð Ñ•Ð¡â‚¬Ð Ñ‘Ð Â±Ð Ñ”Ð Â°"
    if "api_id_invalid" in message:
        return "Ð Ð…Ð ÂµÐ Ð†Ð ÂµÐ¡Ð‚Ð Ð…Ð¡â€¹Ð â„– TELEGRAM_API_ID"
    if "api_id" in message and "invalid" in message:
        return "Ð Ð…Ð ÂµÐ Ð†Ð ÂµÐ¡Ð‚Ð Ð…Ð¡â€¹Ð â„– TELEGRAM_API_ID"
    if "api_hash_invalid" in message:
        return "Ð Ð…Ð ÂµÐ Ð†Ð ÂµÐ¡Ð‚Ð Ð…Ð¡â€¹Ð â„– TELEGRAM_API_HASH"
    if "auth key" in message and ("unregistered" in message or "duplicated" in message):
        return "Ð¡ÐƒÐ ÂµÐ¡ÐƒÐ¡ÐƒÐ Ñ‘Ð¡Ð Ð¡ÐƒÐ Â±Ð¡Ð‚Ð Ñ•Ð¡â‚¬Ð ÂµÐ Ð…Ð Â°"
    if "session revoked" in message or "session password needed" in message:
        return "Ð¡ÐƒÐ ÂµÐ¡ÐƒÐ¡ÐƒÐ Ñ‘Ð¡Ð Ð Ð…Ð ÂµÐ Ò‘Ð ÂµÐ â„–Ð¡ÐƒÐ¡â€šÐ Ð†Ð Ñ‘Ð¡â€šÐ ÂµÐ Â»Ð¡ÐŠÐ Ð…Ð Â°"
    if "phone code" in message or "sign in" in message:
        return "Ð Ð…Ð¡Ñ“Ð Â¶Ð ÂµÐ Ð… Ð Â»Ð Ñ•Ð Ñ–Ð Ñ‘Ð Ð…"
    return message


async def describe_userbot_runtime_status(
    *,
    api_id: str | None = None,
    api_hash: str | None = None,
    session_path: str | None = None,
    timeout_seconds: float = 6.0,
) -> str:
    resolved_api_id = str(USERBOT_API_ID if api_id is None else api_id).strip()
    resolved_api_hash = str(USERBOT_API_HASH if api_hash is None else api_hash).strip()
    resolved_session_path = str(USERBOT_SESSION_PATH if session_path is None else session_path).strip()

    base_status = describe_userbot_status(
        api_id=resolved_api_id,
        api_hash=resolved_api_hash,
        session_path=resolved_session_path,
    )
    if "Ð Ð…Ð Âµ Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…" in base_status or "Ð Ð…Ð¡Ñ“Ð Â¶Ð ÂµÐ Ð… Ð Â»Ð Ñ•Ð Ñ–Ð Ñ‘Ð Ð…" in base_status or "Ð Ñ•Ð¡â‚¬Ð Ñ‘Ð Â±Ð Ñ”Ð Â°" in base_status:
        return base_status

    client: TelegramClient | None = None
    last_error: Exception | None = None
    try:
        for proxy_candidate in resolve_userbot_proxy_candidates():
            client = TelegramClient(
                resolved_session_path,
                int(resolved_api_id),
                resolved_api_hash,
                proxy=proxy_candidate,
                timeout=max(1, int(timeout_seconds)),
                request_retries=USERBOT_REQUEST_RETRIES,
                connection_retries=USERBOT_CONNECTION_RETRIES,
                retry_delay=USERBOT_RETRY_DELAY_SECONDS,
                auto_reconnect=False,
            )
            try:
                await asyncio.wait_for(client.connect(), timeout=timeout_seconds)
                authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=timeout_seconds)
                if not authorized:
                    return _status_line("User-bot", "Ð¡ÐƒÐ ÂµÐ¡ÐƒÐ¡ÐƒÐ Ñ‘Ð¡Ð Ð Ð…Ð Âµ Ð Â°Ð Ð†Ð¡â€šÐ Ñ•Ð¡Ð‚Ð Ñ‘Ð Â·Ð Ñ•Ð Ð†Ð Â°Ð Ð…Ð Â°", f"session={_mask_path_name(resolved_session_path)}")

                me = await asyncio.wait_for(client.get_me(), timeout=timeout_seconds)
                if not me:
                    return _status_line("User-bot", "Ð Ñ•Ð¡â‚¬Ð Ñ‘Ð Â±Ð Ñ”Ð Â°", "Telegram Ð Ð…Ð Âµ Ð Ð†Ð ÂµÐ¡Ð‚Ð Ð…Ð¡Ñ“Ð Â» Ð Ñ—Ð¡Ð‚Ð Ñ•Ð¡â€žÐ Ñ‘Ð Â»Ð¡ÐŠ")

                identity = f"@{me.username}" if getattr(me, "username", None) else f"id={getattr(me, 'id', '?')}"
                return _status_line("User-bot", "Ð Ñ—Ð Ñ•Ð Ò‘Ð Ñ”Ð Â»Ð¡Ð‹Ð¡â€¡Ð ÂµÐ Ð…", identity)
            except Exception as exc:
                last_error = exc
                try:
                    await client.disconnect()
                except Exception:
                    pass
                client = None
        if last_error is not None:
            raise last_error
        raise RuntimeError("userbot_probe_connect_failed")
    except asyncio.TimeoutError:
        return _status_line("User-bot", "Ð¡â€šÐ Â°Ð â„–Ð Ñ˜Ð Â°Ð¡Ñ“Ð¡â€š", "Telegram Ð Ð…Ð Âµ Ð Ñ•Ð¡â€šÐ Ð†Ð ÂµÐ¡â€šÐ Ñ‘Ð Â» Ð Ð†Ð Ñ•Ð Ð†Ð¡Ð‚Ð ÂµÐ Ñ˜Ð¡Ð")
    except Exception as exc:
        return _status_line("User-bot", "Ð Ñ•Ð¡â‚¬Ð Ñ‘Ð Â±Ð Ñ”Ð Â°", _normalize_userbot_probe_error(exc))
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def describe_vk_mini_app_status(
    *,
    app_id: str | None = None,
    service_key: str | None = None,
    secret_key: str | None = None,
) -> str:
    resolved_app_id = str(VK_MINI_APP_APP_ID if app_id is None else app_id).strip()
    resolved_service_key = str(VK_MINI_APP_SERVICE_KEY if service_key is None else service_key).strip()
    resolved_secret_key = str(VK_MINI_APP_SECRET_KEY if secret_key is None else secret_key).strip()

    missing: list[str] = []
    if not resolved_app_id:
        missing.append("app_id")
    if not resolved_service_key:
        missing.append("service_key")
    if not resolved_secret_key:
        missing.append("secret_key")

    if len(missing) == 3:
        return _status_line("VK Mini App", "Ð Ð…Ð Âµ Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…")
    if missing:
        details = []
        if resolved_app_id:
            details.append(f"app_id={resolved_app_id}")
        details.append(f"Ð Ð…Ð ÂµÐ¡â€š: {', '.join(missing)}")
        return _status_line("VK Mini App", "Ð¡â€¡Ð Â°Ð¡ÐƒÐ¡â€šÐ Ñ‘Ð¡â€¡Ð Ð…Ð Ñ• Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…", "; ".join(details))
    return _status_line("VK Mini App", "Ð Ñ–Ð Ñ•Ð¡â€šÐ Ñ•Ð Ð†", f"app_id={resolved_app_id}")


def describe_vk_community_status(
    *,
    community_id: str | None = None,
    access_token: str | None = None,
) -> str:
    resolved_community_id = str(VK_COMMUNITY_ID if community_id is None else community_id).strip()
    resolved_access_token = str(
        VK_COMMUNITY_ACCESS_TOKEN if access_token is None else access_token
    ).strip()

    group_id = _parse_positive_int(resolved_community_id)
    token_configured = bool(resolved_access_token)

    if group_id and token_configured:
        return _status_line("VK Ð¡ÐƒÐ Ñ•Ð Ñ•Ð Â±Ð¡â€°Ð ÂµÐ¡ÐƒÐ¡â€šÐ Ð†Ð Ñ•", "Ð Ñ–Ð Ñ•Ð¡â€šÐ Ñ•Ð Ð†Ð Ñ•", f"group_id={group_id}, token=ok")
    if not group_id and not token_configured:
        return _status_line("VK Ð¡ÐƒÐ Ñ•Ð Ñ•Ð Â±Ð¡â€°Ð ÂµÐ¡ÐƒÐ¡â€šÐ Ð†Ð Ñ•", "Ð Ð…Ð Âµ Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…Ð Ñ•")
    if not group_id:
        return _status_line("VK Ð¡ÐƒÐ Ñ•Ð Ñ•Ð Â±Ð¡â€°Ð ÂµÐ¡ÐƒÐ¡â€šÐ Ð†Ð Ñ•", "Ð Ñ•Ð¡â‚¬Ð Ñ‘Ð Â±Ð Ñ”Ð Â°", "Ð Ð…Ð ÂµÐ¡â€š Ð Ñ”Ð Ñ•Ð¡Ð‚Ð¡Ð‚Ð ÂµÐ Ñ”Ð¡â€šÐ Ð…Ð Ñ•Ð Ñ–Ð Ñ• VK_COMMUNITY_ID")
    return _status_line("VK Ð¡ÐƒÐ Ñ•Ð Ñ•Ð Â±Ð¡â€°Ð ÂµÐ¡ÐƒÐ¡â€šÐ Ð†Ð Ñ•", "Ð¡â€¡Ð Â°Ð¡ÐƒÐ¡â€šÐ Ñ‘Ð¡â€¡Ð Ð…Ð Ñ• Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…Ð Ñ•", f"group_id={group_id}, Ð Ð…Ð ÂµÐ¡â€š access token")


def describe_tech_status_target(*, chat_id: int | None, topic_id: int | None) -> str:
    if chat_id and topic_id:
        return _status_line("Ð ÑžÐ ÂµÐ¡â€¦-Ð¡ÐƒÐ¡â€šÐ Â°Ð¡â€šÐ¡Ñ“Ð¡Ðƒ", "Ð Ñ–Ð Ñ•Ð¡â€šÐ Ñ•Ð Ð†", f"chat_id={chat_id}, topic_id={topic_id}")
    if chat_id:
        return _status_line("Ð ÑžÐ ÂµÐ¡â€¦-Ð¡ÐƒÐ¡â€šÐ Â°Ð¡â€šÐ¡Ñ“Ð¡Ðƒ", "Ð¡â€¡Ð Â°Ð¡ÐƒÐ¡â€šÐ Ñ‘Ð¡â€¡Ð Ð…Ð Ñ• Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…", f"chat_id={chat_id}, Ð Ð…Ð ÂµÐ¡â€š topic_id")
    return _status_line("Ð ÑžÐ ÂµÐ¡â€¦-Ð¡ÐƒÐ¡â€šÐ Â°Ð¡â€šÐ¡Ñ“Ð¡Ðƒ", "Ð Ð…Ð Âµ Ð Ð…Ð Â°Ð¡ÐƒÐ¡â€šÐ¡Ð‚Ð Ñ•Ð ÂµÐ Ð…")


def build_startup_status_text(
    *,
    started_at: datetime | None = None,
    bot_username: str | None = None,
    tech_chat_id: int | None = None,
    tech_status_topic_id: int | None = None,
    userbot_status_line: str | None = None,
) -> str:
    launched_at = started_at or datetime.now()
    normalized_username = str(bot_username or "").strip().lstrip("@")

    lines = [
        "Ð²Ñšâ€¦ Ð Ñ›Ð Â±Ð¡â€°Ð Ñ‘Ð â„– Ð¡ÐƒÐ¡â€šÐ Â°Ð¡â€šÐ¡Ñ“Ð¡Ðƒ Ð¡ÐƒÐ Ñ‘Ð¡ÐƒÐ¡â€šÐ ÂµÐ Ñ˜Ð¡â€¹",
        f"Ð â€”Ð Â°Ð Ñ—Ð¡Ñ“Ð¡ÐƒÐ Ñ”: {launched_at.strftime('%d.%m.%Y %H:%M:%S')}",
        _status_line("Telegram bot", "Ð Ñ–Ð Ñ•Ð¡â€šÐ Ñ•Ð Ð†", f"@{normalized_username}" if normalized_username else "username Ð Ð…Ð Âµ Ð Ñ—Ð Ñ•Ð Â»Ð¡Ñ“Ð¡â€¡Ð ÂµÐ Ð…"),
        describe_tech_status_target(chat_id=tech_chat_id, topic_id=tech_status_topic_id),
        str(userbot_status_line or describe_userbot_status()),
        describe_vk_mini_app_status(),
        describe_vk_community_status(),
    ]
    return "\n".join(lines)
