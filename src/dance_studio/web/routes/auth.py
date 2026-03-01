import secrets
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from dance_studio.core.config import SESSION_TTL_DAYS, TG_INIT_DATA_MAX_AGE_SECONDS
from dance_studio.core.tg_auth import validate_init_data
from dance_studio.core.tg_replay import store_used_init_data
from dance_studio.db.models import SessionRecord
from dance_studio.web.services.auth_session import (
    _clear_sid_cookie,
    _create_session,
    _delete_expired_sessions_for_user,
    _enforce_session_limit,
    _extract_init_data_from_request,
    _extract_ip_prefix,
    _hash_user_agent,
    _set_sid_cookie,
    _sid_hash,
)

bp = Blueprint('auth_routes', __name__)


@bp.route("/auth/telegram", methods=["POST"])
def auth_telegram():
    db = g.db
    init_data = _extract_init_data_from_request()
    if not init_data:
        return {"error": "Authorization initData is required"}, 400

    verified = validate_init_data(init_data)
    if not verified:
        return {"error": "init_data Р Р…Р ВµР Т‘Р ВµР в„–РЎРѓРЎвЂљР Р†Р С‘РЎвЂљР ВµР В»Р ВµР Р…"}, 401

    telegram_id = verified.user_id

    sid = secrets.token_hex(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(days=SESSION_TTL_DAYS)
    user_agent_hash = _hash_user_agent(request.headers.get("User-Agent"))
    ip_prefix = _extract_ip_prefix()

    try:
        replay_ttl = TG_INIT_DATA_MAX_AGE_SECONDS + 60
        if not store_used_init_data(db, verified.replay_key, replay_ttl):
            return {"error": "replay detected", "code": "replay_detected"}, 401

        _delete_expired_sessions_for_user(db, telegram_id)
        _create_session(db, telegram_id, sid, now, expires_at, user_agent_hash, ip_prefix)
        db.flush()
        _enforce_session_limit(db, telegram_id)
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed to create telegram auth session")
        return {"error": "Р СњР Вµ РЎС“Р Т‘Р В°Р В»Р С•РЎРѓРЎРЉ РЎРѓР С•Р В·Р Т‘Р В°РЎвЂљРЎРЉ РЎРѓР ВµРЎРѓРЎРѓР С‘РЎР‹"}, 500

    response = jsonify({"ok": True, "telegram_id": telegram_id})
    _set_sid_cookie(response, sid)
    return response


@bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    db = g.db
    sid = request.cookies.get("sid")
    if sid:
        try:
            db.query(SessionRecord).filter(SessionRecord.sid_hash == _sid_hash(sid)).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            current_app.logger.exception("Failed to logout session")
            return {"error": "Р СњР Вµ РЎС“Р Т‘Р В°Р В»Р С•РЎРѓРЎРЉ Р В·Р В°Р Р†Р ВµРЎР‚РЎв‚¬Р С‘РЎвЂљРЎРЉ РЎРѓР ВµРЎРѓРЎРѓР С‘РЎР‹"}, 500

    response = jsonify({"ok": True})
    _clear_sid_cookie(response)
    return response
