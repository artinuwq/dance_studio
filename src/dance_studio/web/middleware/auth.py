import secrets
from datetime import datetime, timedelta

from flask import Flask, current_app, g, request

from dance_studio.core.config import (
    ROTATE_IF_DAYS_LEFT,
    SESSION_REAUTH_IDLE_SECONDS,
    SESSION_TTL_DAYS,
    TG_INIT_DATA_MAX_AGE_SECONDS,
)
from dance_studio.core.tg_auth import validate_init_data
from dance_studio.core.tg_replay import store_used_init_data
from dance_studio.db import get_session
from dance_studio.db.models import SessionRecord
from dance_studio.web.services.auth_session import (
    CSRF_EXEMPT_PATHS,
    CSRF_EXEMPT_PREFIXES,
    STATE_CHANGING_METHODS,
    _clear_sid_cookie,
    _create_session,
    _enforce_session_limit,
    _extract_init_data_from_request,
    _extract_ip_prefix,
    _is_csrf_valid,
    _is_sensitive_endpoint,
    _set_sid_cookie,
    _sid_hash,
)


def before_request():
    g.db = get_session()
    g.telegram_user = None
    g.telegram_id = None
    g.rotate_sid = None
    g.clear_sid_cookie = False
    g.need_reauth = False

    if request.method in STATE_CHANGING_METHODS and request.path not in CSRF_EXEMPT_PATHS:
        if not any(request.path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            if not _is_csrf_valid():
                return {"error": "CSRF validation failed"}, 403

    sid = request.cookies.get("sid")
    if not sid:
        return

    try:
        db = g.db
        session = db.query(SessionRecord).filter_by(sid_hash=_sid_hash(sid)).first()
        if not session:
            g.clear_sid_cookie = True
            return

        now = datetime.utcnow()
        if session.expires_at <= now:
            db.delete(session)
            db.commit()
            g.clear_sid_cookie = True
            return

        ip_prefix = _extract_ip_prefix()
        should_commit = False

        if session.ip_prefix and ip_prefix and session.ip_prefix != ip_prefix:
            session.need_reauth = True
            session.reauth_reason = "ip_prefix_changed"
            should_commit = True

        if session.last_seen and (now - session.last_seen).total_seconds() > SESSION_REAUTH_IDLE_SECONDS:
            session.need_reauth = True
            session.reauth_reason = session.reauth_reason or "idle_timeout"
            should_commit = True

        if session.need_reauth and _is_sensitive_endpoint():
            init_data = _extract_init_data_from_request()
            if not init_data:
                return {"error": "need_reauth", "code": "need_reauth"}, 401

            verified = validate_init_data(init_data)
            if not verified or verified.user_id != session.telegram_id:
                return {"error": "need_reauth", "code": "need_reauth"}, 401

            replay_ttl = TG_INIT_DATA_MAX_AGE_SECONDS + 60
            if not store_used_init_data(db, verified.replay_key, replay_ttl):
                return {"error": "replay detected", "code": "replay_detected"}, 401

            new_sid = secrets.token_hex(32)
            new_expires_at = now + timedelta(days=SESSION_TTL_DAYS)
            _create_session(db, session.telegram_id, new_sid, now, new_expires_at, session.user_agent_hash, ip_prefix)
            db.delete(session)
            db.flush()
            _enforce_session_limit(db, session.telegram_id)
            g.rotate_sid = new_sid
            should_commit = True

            session = db.query(SessionRecord).filter_by(sid_hash=_sid_hash(new_sid)).first()

        telegram_id = session.telegram_id
        session.last_seen = now
        session.ip_prefix = ip_prefix or session.ip_prefix

        if session.expires_at - now < timedelta(days=ROTATE_IF_DAYS_LEFT):
            new_sid = secrets.token_hex(32)
            new_expires_at = now + timedelta(days=SESSION_TTL_DAYS)
            _create_session(db, session.telegram_id, new_sid, now, new_expires_at, session.user_agent_hash, session.ip_prefix)
            db.delete(session)
            db.flush()
            _enforce_session_limit(db, session.telegram_id)
            g.rotate_sid = new_sid
            should_commit = True
        else:
            should_commit = True

        if should_commit:
            db.commit()

        g.telegram_id = telegram_id
        g.telegram_user = {"id": telegram_id}
    except Exception:
        g.db.rollback()
        current_app.logger.exception("Session validation failed")
        g.clear_sid_cookie = True
        return


def teardown_request(exception):
    db = getattr(g, 'db', None)
    if db is not None:
        db.close()


def refresh_sid_cookie(response):
    if getattr(g, "clear_sid_cookie", False):
        _clear_sid_cookie(response)
    rotate_sid = getattr(g, "rotate_sid", None)
    if rotate_sid:
        _set_sid_cookie(response, rotate_sid)
    return response


def register_auth_middleware(app: Flask) -> None:
    app.before_request(before_request)
    app.teardown_request(teardown_request)
    app.after_request(refresh_sid_cookie)

