import secrets
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from dance_studio.auth.providers.passkey import PasskeyAuthProvider
from dance_studio.auth.providers.phone import PhoneCodeAuthProvider
from dance_studio.auth.providers.telegram import TelegramAuthProvider
from dance_studio.auth.providers.vk import VkMiniAppAuthProvider
from dance_studio.auth.services.account_merge import AccountMergeService
from dance_studio.auth.services.audit import log_auth_event
from dance_studio.auth.services.bootstrap import build_user_auth_contract
from dance_studio.auth.services.common import (
    DuplicateIdentityError,
    ManualMergeRequiredError,
    VerifiedPhoneConflictError,
    normalize_phone_e164,
)
from dance_studio.auth.services.contracts import (
    DEFAULT_FALLBACK_AUTH_METHODS,
    auth_error_payload,
    link_success_payload,
)
from dance_studio.auth.services.rate_limit import RateLimitExceededError, hit_rate_limit
from dance_studio.core.config import SESSION_TTL_DAYS, TG_INIT_DATA_MAX_AGE_SECONDS
from dance_studio.core.tg_replay import store_used_init_data
from dance_studio.db.models import AuthIdentity, NotificationChannel, SessionRecord, User
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

MERGE_CONFLICT_NOTICE = "Мы нашли несколько аккаунтов с этим номером. Напишите в поддержку, чтобы объединить их."
MANUAL_MERGE_NOTICE = "Мы нашли данные, которые требуют ручной проверки перед объединением аккаунтов. Напишите в поддержку."


def _rate_limit_subject() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() or request.remote_addr or "unknown"
    return ip


def _request_origin() -> str:
    return request.headers.get("Origin") or request.headers.get("X-Forwarded-Origin") or request.host_url.rstrip("/")


def _auth_error_response(error: str, status: int, **extra):
    return auth_error_payload(error, **extra), status


def _handle_auth_provider_error(error: Exception):
    if isinstance(error, RateLimitExceededError):
        return _auth_error_response("rate_limited", 429, message="Слишком много попыток. Попробуйте позже.")
    if isinstance(error, VerifiedPhoneConflictError):
        return _auth_error_response(
            "verified_phone_conflict",
            409,
            message=MERGE_CONFLICT_NOTICE,
            action="contact_support",
            fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS,
            conflict_user_ids=error.user_ids,
        )
    if isinstance(error, ManualMergeRequiredError):
        return _auth_error_response(
            "manual_merge_required",
            409,
            message=MANUAL_MERGE_NOTICE,
            action="contact_support",
            fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS,
        )
    if isinstance(error, DuplicateIdentityError):
        return _auth_error_response(
            "identity_already_linked",
            409,
            message="Этот способ входа уже привязан к другому аккаунту.",
            action="switch_account",
            fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS,
        )
    raise error


def _login_user(db, *, user_id: int, telegram_id: int | None, extra_payload: dict | None = None):
    sid = secrets.token_hex(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(days=SESSION_TTL_DAYS)
    user_agent_hash = _hash_user_agent(request.headers.get("User-Agent"))
    ip_prefix = _extract_ip_prefix()

    _delete_expired_sessions_for_user(db, user_id=user_id)
    _create_session(db, telegram_id, sid, now, expires_at, user_agent_hash, ip_prefix, user_id=user_id)
    db.flush()
    _enforce_session_limit(db, user_id=user_id)

    payload = {"ok": True, "user_id": user_id, "telegram_id": telegram_id}
    if extra_payload:
        payload.update(extra_payload)
    response = jsonify(payload)
    _set_sid_cookie(response, sid)
    _set_csrf_cookie(response)
    return response


def _auth_success_response(db, *, user: User, provider: str, link_mode: bool, extra_payload: dict | None = None):
    if link_mode:
        payload = link_success_payload(
            provider=provider,
            user_id=user.id,
            identities=(build_user_auth_contract(db, user) or {}).get("identities"),
        )
        if extra_payload:
            payload.update(extra_payload)
        return payload
    return _login_user(db, user_id=user.id, telegram_id=user.telegram_id, extra_payload=extra_payload)


def _merge_payload_from_result(merge_result: dict | None) -> dict:
    merge_result = merge_result or {}
    merge_status = merge_result.get("status")
    payload: dict = {}
    if merge_status == "merged":
        payload["merge_notice"] = "Аккаунты объединены. Проверьте, что все данные на месте."
    elif merge_status == "conflict":
        payload["merge_notice"] = MERGE_CONFLICT_NOTICE
    elif merge_status == "manual_review_required":
        payload["merge_notice"] = MANUAL_MERGE_NOTICE
    if merge_status:
        payload["merge_status"] = merge_status
    if merge_result.get("conflict_user_ids"):
        payload["conflict_user_ids"] = merge_result["conflict_user_ids"]
    return payload


def _resolve_passkey_provider_context():
    provider = PasskeyAuthProvider()
    origin, rp_id = provider.resolve_origin_and_rp_id(origin=_request_origin(), rp_id=request.headers.get("X-Webauthn-Rp-Id"))
    return provider, origin, rp_id


@bp.route("/auth/telegram", methods=["POST"])
def auth_telegram():
    db = g.db
    init_data = _extract_init_data_from_request()
    if not init_data:
        return _auth_error_response("init_data_required", 400)
    try:
        hit_rate_limit("telegram_login", _rate_limit_subject())
        provider = TelegramAuthProvider()
        payload = request.get_json(silent=True) or {}
        current_user_id = getattr(g, "user_id", None)
        user, error = provider.authenticate(
            db,
            init_data,
            current_user_id=current_user_id,
            verified_phone=payload.get("phone") if payload.get("phone_verified") else None,
        )
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        raise
    if error:
        return _auth_error_response(error, 401, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)

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
            return _auth_error_response("replay_detected", 401)

        if verified_identity:
            verified_identity.last_login_at = datetime.utcnow()
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

        link_mode = current_user_id is not None
        response = _auth_success_response(db, user=user, provider="telegram", link_mode=link_mode)
        log_auth_event(db, event_type=("telegram_link" if link_mode else "telegram_login"), provider="telegram", user_id=user.id)
        db.commit()
        return response
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed to create telegram auth session")
        return _auth_error_response("session_create_failed", 500)


@bp.route("/auth/vk", methods=["POST"])
def auth_vk():
    db = g.db
    payload = request.get_json(silent=True) or {}
    current_user_id = getattr(g, "user_id", None)
    try:
        hit_rate_limit("vk_login", _rate_limit_subject())
        provider = VkMiniAppAuthProvider()
        user, error = provider.authenticate(db, payload, current_user_id=current_user_id)
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        raise
    if error:
        return _auth_error_response(error, 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    try:
        vk_user_id = str(payload.get("vk_user_id") or payload.get("user_id"))
        identity = db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id, AuthIdentity.provider == "vk", AuthIdentity.provider_user_id == vk_user_id).first()
        if identity:
            identity.last_login_at = datetime.utcnow()
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

        link_mode = current_user_id is not None
        response = _auth_success_response(db, user=user, provider="vk", link_mode=link_mode)
        log_auth_event(db, event_type=("vk_link" if link_mode else "vk_login"), provider="vk", user_id=user.id)
        db.commit()
        return response
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed VK auth")
        return _auth_error_response("vk_auth_failed", 500)


@bp.route("/auth/vk/phone", methods=["POST"])
def auth_vk_phone():
    db = g.db
    payload = request.get_json(silent=True) or {}
    phone = str(payload.get("phone") or payload.get("phone_number") or "").strip()
    if not phone:
        return _auth_error_response("phone_required", 400)

    user_id = getattr(g, "user_id", None)
    if not user_id:
        return _auth_error_response("auth_required", 401)

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        return _auth_error_response("user_not_found", 404)

    try:
        merge_result = AccountMergeService().try_merge_by_phone(db, user_id=user.id, phone=phone, source="vk_phone")
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        current_app.logger.exception("Failed to auto-merge accounts by phone")
        return _auth_error_response("vk_phone_update_failed", 500)

    try:
        response_payload = {"ok": True, "phone": normalize_phone_e164(phone)}
        response_payload.update(_merge_payload_from_result(merge_result))
        db.commit()
        return response_payload
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed VK phone update")
        return _auth_error_response("vk_phone_update_failed", 500)


@bp.route("/auth/phone/request-code", methods=["POST"])
def request_phone_code():
    db = g.db
    payload = request.get_json(silent=True) or {}
    phone = str(payload.get("phone") or "").strip()
    if not phone:
        return _auth_error_response("phone_required", 400)
    provider = PhoneCodeAuthProvider()
    try:
        hit_rate_limit("otp_request", f"{_rate_limit_subject()}:{normalize_phone_e164(phone) or phone}")
        code = provider.request_code(db, phone=phone, purpose=str(payload.get("purpose") or "login"))
    except ValueError as exc:
        db.rollback()
        return _auth_error_response(str(exc), 400)
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        raise
    db.commit()
    return {"ok": True, "delivery": "internal", "debug_code": code}


@bp.route("/auth/phone/verify-code", methods=["POST"])
def verify_phone_code():
    db = g.db
    payload = request.get_json(silent=True) or {}
    phone = str(payload.get("phone") or "").strip()
    code = str(payload.get("code") or "").strip()
    if not phone or not code:
        return _auth_error_response("phone_and_code_required", 400)

    current_user_id = getattr(g, "user_id", None)
    provider = PhoneCodeAuthProvider()
    try:
        hit_rate_limit("otp_verify", f"{_rate_limit_subject()}:{normalize_phone_e164(phone) or phone}")
        user, error = provider.verify_code(db, phone=phone, code=code, current_user_id=current_user_id)
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        raise
    if error:
        db.rollback()
        return _auth_error_response(error, 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    try:
        merge_result = AccountMergeService().try_merge_by_phone(db, user_id=user.id, phone=phone, source="phone_verification")
        payload_extra = _merge_payload_from_result(merge_result)
        login_user_id = int((merge_result or {}).get("primary_user_id") or user.id)
        login_user = db.query(User).filter(User.id == login_user_id).first() or user
        link_mode = current_user_id is not None
        response = _auth_success_response(
            db,
            user=login_user,
            provider="phone",
            link_mode=link_mode,
            extra_payload=(payload_extra or None),
        )
        log_auth_event(db, event_type=("phone_link" if link_mode else "phone_login"), provider="phone", user_id=login_user.id)
        db.commit()
        return response
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        db.rollback()
        current_app.logger.exception("Failed phone auth")
        return _auth_error_response("phone_auth_failed", 500)


@bp.route("/auth/passkey/register/begin", methods=["POST"])
def passkey_register_begin():
    db = g.db
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return _auth_error_response("auth_required", 401, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    provider, origin, rp_id = _resolve_passkey_provider_context()
    if not origin or not rp_id:
        return _auth_error_response("invalid_passkey_origin", 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    payload = provider.register_begin(db, user_id=int(user_id), session_user_id=int(user_id), origin=origin, rp_id=rp_id)
    db.commit()
    return payload


@bp.route("/auth/passkey/register/complete", methods=["POST"])
def passkey_register_complete():
    db = g.db
    user_id = getattr(g, "user_id", None)
    provider, origin, rp_id = _resolve_passkey_provider_context()
    if not origin or not rp_id:
        return _auth_error_response("invalid_passkey_origin", 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    credential, error = provider.register_complete(
        db,
        user_id=int(user_id or 0),
        payload=request.get_json(silent=True) or {},
        origin=origin,
        rp_id=rp_id,
    )
    if error:
        db.rollback()
        return _auth_error_response(error, 400 if error != "auth_required" else 401, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    db.commit()
    return {"ok": True, "credential_id": credential.credential_id}


@bp.route("/auth/passkeys", methods=["GET"])
def passkey_list():
    db = g.db
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return _auth_error_response("auth_required", 401, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    provider = PasskeyAuthProvider()
    items = provider.list_credentials(db, user_id=int(user_id))
    return {
        "items": [
            {
                "credential_id": row.credential_id,
                "device_name": row.device_name,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            }
            for row in items
        ]
    }


@bp.route("/auth/passkey/delete", methods=["POST"])
def passkey_delete():
    db = g.db
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return _auth_error_response("auth_required", 401, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    payload = request.get_json(silent=True) or {}
    credential_id = str(payload.get("credential_id") or "").strip()
    if not credential_id:
        return _auth_error_response("credential_id_required", 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    provider = PasskeyAuthProvider()
    deleted = provider.delete_credential(db, user_id=int(user_id), credential_id=credential_id)
    if not deleted:
        db.rollback()
        return _auth_error_response("credential_not_found", 404, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    db.commit()
    return {"ok": True}


@bp.route("/auth/passkey/login/begin", methods=["POST"])
def passkey_login_begin():
    db = g.db
    provider, origin, rp_id = _resolve_passkey_provider_context()
    if not origin or not rp_id:
        return _auth_error_response("invalid_passkey_origin", 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    payload = provider.login_begin(db, origin=origin, rp_id=rp_id)
    db.commit()
    return payload


@bp.route("/auth/passkey/login/complete", methods=["POST"])
def passkey_login_complete():
    db = g.db
    provider, origin, rp_id = _resolve_passkey_provider_context()
    if not origin or not rp_id:
        return _auth_error_response("invalid_passkey_origin", 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    try:
        hit_rate_limit("passkey_login", _rate_limit_subject())
        credential, error = provider.login_complete(db, request.get_json(silent=True) or {}, origin=origin, rp_id=rp_id)
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        raise
    if error:
        db.rollback()
        return _auth_error_response(error, 400, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    user = db.query(User).filter(User.id == credential.user_id).first()
    if not user:
        db.rollback()
        return _auth_error_response("user_not_found", 404, fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS)
    response = _login_user(db, user_id=user.id, telegram_id=user.telegram_id, extra_payload={"passkey": True})
    db.commit()
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
            return {"error": "Не удалось завершить сессию"}, 500

    response = jsonify({"ok": True})
    _clear_sid_cookie(response)
    _clear_csrf_cookie(response)
    return response
