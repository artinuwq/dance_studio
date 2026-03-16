import secrets
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from dance_studio.auth.providers.passkey import PasskeyAuthProvider
from dance_studio.auth.providers.phone import PhoneCodeAuthProvider
from dance_studio.auth.providers.telegram import TelegramAuthProvider
from dance_studio.auth.providers.vk import VkMiniAppAuthProvider
from dance_studio.core.config import SESSION_TTL_DAYS, TG_INIT_DATA_MAX_AGE_SECONDS
from dance_studio.core.tg_replay import store_used_init_data
from dance_studio.db.models import AuthIdentity, NotificationChannel, SessionRecord
from dance_studio.web.services.auth_session import (
    _clear_csrf_cookie,
    _clear_sid_cookie,
    _create_session,
    _delete_expired_sessions_for_user,
    _enforce_session_limit,
    _extract_init_data_from_request,
    _extract_ip_prefix,
    _hash_user_agent,
    _set_csrf_cookie,
    _set_sid_cookie,
    _sid_hash,
)

bp = Blueprint('auth_routes', __name__)


def _login_user(db, *, user_id: int, telegram_id: int | None):
    sid = secrets.token_hex(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(days=SESSION_TTL_DAYS)
    user_agent_hash = _hash_user_agent(request.headers.get("User-Agent"))
    ip_prefix = _extract_ip_prefix()

    _delete_expired_sessions_for_user(db, user_id=user_id)
    _create_session(db, telegram_id, sid, now, expires_at, user_agent_hash, ip_prefix, user_id=user_id)
    db.flush()
    _enforce_session_limit(db, user_id=user_id)

    response = jsonify({"ok": True, "user_id": user_id, "telegram_id": telegram_id})
    _set_sid_cookie(response, sid)
    _set_csrf_cookie(response)
    return response


@bp.route("/auth/telegram", methods=["POST"])
def auth_telegram():
    db = g.db
    init_data = _extract_init_data_from_request()
    if not init_data:
        return {"error": "Authorization initData is required"}, 400

    provider = TelegramAuthProvider()
    user, error = provider.authenticate(db, init_data)
    if error:
        return {"error": error}, 401

    try:
        replay_ttl = TG_INIT_DATA_MAX_AGE_SECONDS + 60
        verified_identity = (
            db.query(AuthIdentity)
            .filter(AuthIdentity.user_id == user.id, AuthIdentity.provider == "telegram")
            .order_by(AuthIdentity.id.desc())
            .first()
        )
        replay_key = f"tg:{verified_identity.provider_user_id}:{int(datetime.utcnow().timestamp())}" if verified_identity else None
        if replay_key and not store_used_init_data(db, replay_key, replay_ttl):
            return {"error": "replay detected", "code": "replay_detected"}, 401

        if user.telegram_id:
            channel = db.query(NotificationChannel).filter(
                NotificationChannel.channel_type == "telegram",
                NotificationChannel.target_ref == str(user.telegram_id),
            ).first()
            if not channel:
                channel = NotificationChannel(user_id=user.id, channel_type="telegram", target_ref=str(user.telegram_id))
                db.add(channel)
            channel.is_enabled = True
            channel.is_verified = True
            channel.is_primary = True
        response = _login_user(db, user_id=user.id, telegram_id=user.telegram_id)
        db.commit()
        return response
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed to create telegram auth session")
        return {"error": "Не удалось создать сессию"}, 500


@bp.route("/auth/vk", methods=["POST"])
def auth_vk():
    db = g.db
    payload = request.get_json(silent=True) or {}
    provider = VkMiniAppAuthProvider()
    user, error = provider.authenticate(db, payload)
    if error:
        return {"error": error}, 400
    try:
        vk_user_id = str(payload.get("vk_user_id") or payload.get("user_id"))
        channel = db.query(NotificationChannel).filter(
            NotificationChannel.channel_type == "vk",
            NotificationChannel.target_ref == vk_user_id,
        ).first()
        if not channel:
            channel = NotificationChannel(user_id=user.id, channel_type="vk", target_ref=vk_user_id)
            db.add(channel)
        channel.is_enabled = True
        channel.is_verified = True
        channel.is_primary = False
        response = _login_user(db, user_id=user.id, telegram_id=user.telegram_id)
        db.commit()
        return response
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed VK auth")
        return {"error": "vk auth failed"}, 500


@bp.route("/auth/phone/request-code", methods=["POST"])
def request_phone_code():
    db = g.db
    payload = request.get_json(silent=True) or {}
    phone = str(payload.get("phone") or "").strip()
    if not phone:
        return {"error": "phone required"}, 400
    provider = PhoneCodeAuthProvider()
    code = provider.request_code(db, phone=phone, purpose=str(payload.get("purpose") or "login"))
    db.commit()
    return {"ok": True, "delivery": "internal", "debug_code": code}


@bp.route("/auth/phone/verify-code", methods=["POST"])
def verify_phone_code():
    db = g.db
    payload = request.get_json(silent=True) or {}
    phone = str(payload.get("phone") or "").strip()
    code = str(payload.get("code") or "").strip()
    if not phone or not code:
        return {"error": "phone_and_code_required"}, 400

    provider = PhoneCodeAuthProvider()
    user, error = provider.verify_code(db, phone=phone, code=code)
    if error:
        db.rollback()
        return {"error": error}, 400
    try:
        response = _login_user(db, user_id=user.id, telegram_id=user.telegram_id)
        db.commit()
        return response
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed phone auth")
        return {"error": "phone auth failed"}, 500


@bp.route("/auth/passkey/register/begin", methods=["POST"])
def passkey_register_begin():
    payload = request.get_json(silent=True) or {}
    user_id = int(payload.get("user_id") or 0)
    provider = PasskeyAuthProvider()
    return provider.register_begin(user_id)


@bp.route("/auth/passkey/register/complete", methods=["POST"])
def passkey_register_complete():
    provider = PasskeyAuthProvider()
    return provider.register_complete(request.get_json(silent=True) or {})


@bp.route("/auth/passkey/login/begin", methods=["POST"])
def passkey_login_begin():
    provider = PasskeyAuthProvider()
    return provider.login_begin()


@bp.route("/auth/passkey/login/complete", methods=["POST"])
def passkey_login_complete():
    provider = PasskeyAuthProvider()
    return provider.login_complete(request.get_json(silent=True) or {})


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
            return {"error": "Не удалось завершить сессию"}, 500

    response = jsonify({"ok": True})
    _clear_sid_cookie(response)
    _clear_csrf_cookie(response)
    return response
