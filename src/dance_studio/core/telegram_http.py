from __future__ import annotations

from typing import Any

import requests

from dance_studio.core.config import BOT_TOKEN
from dance_studio.core.settings import BACKUP_TELEGRAM_PROXY, TELEGRAM_PROXY


def _normalize_proxy_url(proxy: str) -> str:
    normalized = str(proxy or "").strip()
    if normalized.startswith("socks5://"):
        return "socks5h://" + normalized[len("socks5://"):]
    if normalized.startswith("socks4://"):
        return "socks4a://" + normalized[len("socks4://"):]
    return normalized


def resolve_telegram_requests_proxy() -> str | None:
    proxy = _normalize_proxy_url(TELEGRAM_PROXY or BACKUP_TELEGRAM_PROXY or "")
    return proxy or None


def _build_requests_session(timeout: int) -> tuple[requests.Session, dict[str, Any]]:
    request_kwargs: dict[str, Any] = {
        "timeout": timeout,
    }
    session = requests.Session()
    proxy = resolve_telegram_requests_proxy()
    if proxy:
        # Prefer the explicitly configured Telegram proxy over ambient env proxies.
        session.trust_env = False
        request_kwargs["proxies"] = {
            "http": proxy,
            "https": proxy,
        }
    return session, request_kwargs


def telegram_api_post(
    method: str,
    payload: dict[str, Any],
    *,
    timeout: int = 10,
) -> tuple[bool, dict[str, Any], str | None]:
    if not BOT_TOKEN:
        return False, {}, "telegram_not_configured"

    try:
        session, request_kwargs = _build_requests_session(timeout)
        with session:
            request_kwargs["json"] = payload
            response = session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                **request_kwargs,
            )
    except Exception as exc:
        return False, {}, f"telegram_exception:{exc}"

    try:
        data = response.json() if response.content else {}
    except Exception:
        data = {}

    if not response.ok:
        description = str((data or {}).get("description") or response.text or "").strip()
        return False, data if isinstance(data, dict) else {}, f"telegram_http_{response.status_code}:{description or 'send_failed'}"

    if not bool((data or {}).get("ok")):
        description = str((data or {}).get("description") or "").strip()
        return False, data if isinstance(data, dict) else {}, f"telegram_api:{description or 'send_failed'}"

    return True, data if isinstance(data, dict) else {}, None


def telegram_api_get(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = 10,
) -> tuple[bool, dict[str, Any], str | None]:
    if not BOT_TOKEN:
        return False, {}, "telegram_not_configured"

    try:
        session, request_kwargs = _build_requests_session(timeout)
        with session:
            if params is not None:
                request_kwargs["params"] = params
            response = session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                **request_kwargs,
            )
    except Exception as exc:
        return False, {}, f"telegram_exception:{exc}"

    try:
        data = response.json() if response.content else {}
    except Exception:
        data = {}

    if not response.ok:
        description = str((data or {}).get("description") or response.text or "").strip()
        return False, data if isinstance(data, dict) else {}, f"telegram_http_{response.status_code}:{description or 'request_failed'}"

    if not bool((data or {}).get("ok")):
        description = str((data or {}).get("description") or "").strip()
        return False, data if isinstance(data, dict) else {}, f"telegram_api:{description or 'request_failed'}"

    return True, data if isinstance(data, dict) else {}, None


def telegram_api_download_file(
    file_path: str,
    *,
    timeout: int = 15,
) -> tuple[bool, bytes, str | None, str | None]:
    normalized_file_path = str(file_path or "").strip().lstrip("/")
    if not BOT_TOKEN:
        return False, b"", None, "telegram_not_configured"
    if not normalized_file_path:
        return False, b"", None, "telegram_file_path_required"

    try:
        session, request_kwargs = _build_requests_session(timeout)
        with session:
            response = session.get(
                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{normalized_file_path}",
                **request_kwargs,
            )
    except Exception as exc:
        return False, b"", None, f"telegram_exception:{exc}"

    if not response.ok:
        description = str(response.text or "").strip()
        return False, b"", None, f"telegram_http_{response.status_code}:{description or 'download_failed'}"

    content = response.content or b""
    if not content:
        return False, b"", None, "telegram_empty_file"

    return True, content, (response.headers.get("Content-Type") or None), None


__all__ = [
    "telegram_api_download_file",
    "telegram_api_get",
    "resolve_telegram_requests_proxy",
    "telegram_api_post",
]
