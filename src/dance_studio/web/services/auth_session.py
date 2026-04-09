from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from dance_studio.core.time import utcnow
from urllib.parse import urlparse

from flask import request

from dance_studio.core.config import (
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    CSRF_TRUSTED_ORIGINS,
    MAX_SESSIONS_PER_USER,
    SESSION_PEPPER,
    SESSION_TTL_DAYS,
    WEB_APP_URL,
)
from dance_studio.db.models import SessionRecord

SESSION_TTL_SECONDS = SESSION_TTL_DAYS * 24 * 3600
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PATHS = {"/auth/telegram", "/auth/vk", "/auth/phone/request-code", "/auth/phone/verify-code", "/auth/passkey/register/begin", "/auth/passkey/register/complete", "/auth/passkey/login/begin", "/auth/passkey/login/complete", "/auth/logout", "/health", "/api/vk/callback", "/csp-report"}
CSRF_EXEMPT_PREFIXES = ("/api/directions/photo/",)
SENSITIVE_PATH_PREFIXES = ("/schedule", "/api/bookings", "/api/payments", "/mailings", "/news")
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAMES = ("X-CSRF-Token", "X-XSRF-Token")

def _hash_user_agent(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()

def _extract_ip_prefix() -> str | None:
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "").strip()
    if not ip:
        return None
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    if ":" in ip:
        return ":".join(ip.split(":")[:4])
    return ip

def _is_sensitive_endpoint() -> bool:
    return request.path.startswith(SENSITIVE_PATH_PREFIXES)

def _extract_init_data_from_request() -> str | None:
    # Accept both legacy and new header names so the WebApp can send either.
    header_data = request.headers.get("X-TG-Init-Data", "").strip()
    if not header_data:
        header_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if header_data:
        return header_data

    auth_data = _get_init_data_from_auth_header()
    if auth_data:
        return auth_data

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        body_data = payload.get("init_data") or payload.get("initData")
        if isinstance(body_data, str) and body_data.strip():
            return body_data.strip()
    return None

def _create_session(
    db,
    telegram_id: int | None,
    sid: str,
    now: datetime,
    expires_at: datetime,
    user_agent_hash: str | None,
    ip_prefix: str | None,
    user_id: int | None = None,
) -> None:
    db.add(SessionRecord(
        id=secrets.token_hex(32),
        sid_hash=_sid_hash(sid),
        telegram_id=telegram_id,
        user_id=user_id,
        user_agent_hash=user_agent_hash,
        ip_prefix=ip_prefix,
        need_reauth=False,
        reauth_reason=None,
        created_at=now,
        last_seen=now,
        expires_at=expires_at,
    ))

def _sid_hash(sid: str) -> str:
    return hashlib.sha256(f"{sid}:{SESSION_PEPPER}".encode("utf-8")).hexdigest()

def _origin_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"

def _normalize_origin(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip().rstrip("/")
    if not value:
        return None

    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"

def _build_csrf_trusted_origins() -> set[str]:
    trusted: set[str] = set()

    web_origin = _origin_from_url(WEB_APP_URL)
    if web_origin:
        trusted.add(web_origin)

    for origin in CSRF_TRUSTED_ORIGINS.split(','):
        normalized = _normalize_origin(origin)
        if normalized:
            trusted.add(normalized)

    return trusted

def _extract_request_origin() -> str | None:
    origin = request.headers.get("Origin", "").strip()
    if origin:
        return _normalize_origin(origin)

    referer = request.headers.get("Referer", "").strip()
    if referer:
        return _origin_from_url(referer)

    return None


def _is_csrf_origin_valid() -> bool:
    trusted = _build_csrf_trusted_origins()
    if not trusted:
        return False

    request_origin = _extract_request_origin()
    if not request_origin:
        return False

    return request_origin in trusted


def _extract_csrf_header_token() -> str:
    for header_name in CSRF_HEADER_NAMES:
        token = request.headers.get(header_name, "").strip()
        if token:
            return token
    return ""


def _is_double_submit_token_valid() -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "").strip()
    header_token = _extract_csrf_header_token()
    if not cookie_token or not header_token:
        return False
    return secrets.compare_digest(cookie_token, header_token)


def _is_csrf_valid() -> bool:
    if not _is_csrf_origin_valid():
        return False

    sid = request.cookies.get("sid", "").strip()
    if not sid:
        return False

    return _is_double_submit_token_valid()

def _delete_expired_sessions_for_user(db, telegram_id: int | None = None, user_id: int | None = None) -> None:
    q = db.query(SessionRecord)
    if user_id is not None:
        q = q.filter(SessionRecord.user_id == user_id)
    elif telegram_id is not None:
        q = q.filter(SessionRecord.telegram_id == telegram_id)
    else:
        return
    q.filter(
        SessionRecord.expires_at < utcnow(),
    ).delete(synchronize_session=False)


def _enforce_session_limit(db, telegram_id: int | None = None, user_id: int | None = None) -> None:
    q = db.query(SessionRecord)
    if user_id is not None:
        q = q.filter(SessionRecord.user_id == user_id)
    elif telegram_id is not None:
        q = q.filter(SessionRecord.telegram_id == telegram_id)
    else:
        return
    sessions = q.order_by(SessionRecord.created_at.desc()).all()
    stale = sessions[MAX_SESSIONS_PER_USER:]
    for rec in stale:
        db.delete(rec)

def _set_sid_cookie(response, sid: str) -> None:
    response.set_cookie(
        "sid",
        sid,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def _set_csrf_cookie(response, token: str | None = None) -> str:
    csrf_token = token or secrets.token_hex(32)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )
    return csrf_token


def _clear_sid_cookie(response) -> None:
    response.set_cookie(
        "sid",
        "",
        max_age=0,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def _clear_csrf_cookie(response) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        "",
        max_age=0,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def _get_init_data_from_auth_header() -> str | None:
    auth_header = request.headers.get("Authorization", "").strip()
    if not auth_header:
        return None
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return auth_header

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_EXEMPT_PATHS",
    "CSRF_EXEMPT_PREFIXES",
    "SENSITIVE_PATH_PREFIXES",
    "STATE_CHANGING_METHODS",
    "_clear_csrf_cookie",
    "_clear_sid_cookie",
    "_create_session",
    "_delete_expired_sessions_for_user",
    "_enforce_session_limit",
    "_extract_init_data_from_request",
    "_extract_ip_prefix",
    "_hash_user_agent",
    "_is_csrf_valid",
    "_is_sensitive_endpoint",
    "_set_csrf_cookie",
    "_set_sid_cookie",
    "_sid_hash",
]

