import json
import secrets
from datetime import timedelta

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
from dance_studio.auth.services.otp_delivery import send_phone_otp
from dance_studio.auth.services.rate_limit import RateLimitExceededError, hit_rate_limit
from dance_studio.core.config import ENV, SESSION_TTL_DAYS, TG_INIT_DATA_MAX_AGE_SECONDS
from dance_studio.core.tg_replay import store_used_init_data
from dance_studio.core.time import utcnow
from dance_studio.db import sync_bootstrap_staff_assignment_for_user
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

bp = Blueprint("auth_routes", __name__)
TELEGRAM_REPLAY_IDEMPOTENT_WINDOW_SECONDS = 15

MERGE_CONFLICT_NOTICE = "Мы нашли несколько аккаунтов с этим номером. Напишите в поддержку, чтобы объединить их."
MANUAL_MERGE_NOTICE = "Мы нашли данные, которые требуют ручной проверки перед объединением аккаунтов. Напишите в поддержку."


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_link_mode_requested(payload: dict | None) -> bool:
    payload = payload or {}
    if _as_bool(payload.get("link_mode")) or _as_bool(payload.get("link")):
        return True
    return False


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


def _extract_identity_replay_key(identity: AuthIdentity | None) -> str | None:
    if not identity:
        return None
    payload_json = identity.provider_payload_json
    if isinstance(payload_json, str) and payload_json.strip():
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        replay_key = str(payload.get("replay_key") or "").strip()
        if replay_key:
            return replay_key
    provider_user_id = str(identity.provider_user_id or "").strip()
    return provider_user_id or None


def _has_recent_session_for_same_client(
    db,
    *,
    user_id: int,
    user_agent_hash: str | None,
    ip_prefix: str | None,
    window_seconds: int = TELEGRAM_REPLAY_IDEMPOTENT_WINDOW_SECONDS,
) -> bool:
    now = utcnow()
    threshold = now - timedelta(seconds=window_seconds)
    query = db.query(SessionRecord).filter(
        SessionRecord.user_id == user_id,
        SessionRecord.created_at >= threshold,
        SessionRecord.expires_at > now,
    )
    if user_agent_hash:
        query = query.filter(SessionRecord.user_agent_hash == user_agent_hash)
    if ip_prefix:
        query = query.filter(SessionRecord.ip_prefix == ip_prefix)
    return query.order_by(SessionRecord.created_at.desc()).first() is not None


def _login_user(db, *, user_id: int, telegram_id: int | None, extra_payload: dict | None = None):
    sid = secrets.token_hex(32)
    now = utcnow()
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
    payload = request.get_json(silent=True) or {}
    link_mode_requested = _is_link_mode_requested(payload)
    current_session_user_id = getattr(g, "user_id", None)
    current_user_id = current_session_user_id if link_mode_requested else None
    try:
        hit_rate_limit("telegram_login", _rate_limit_subject())
        provider = TelegramAuthProvider()
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
        link_mode = bool(link_mode_requested and current_session_user_id and int(current_session_user_id) == int(user.id))
        replay_key = _extract_identity_replay_key(verified_identity)
        replay_token = f"tg_login:{replay_key}" if replay_key else None
        if replay_token and not store_used_init_data(db, replay_token, replay_ttl):
            user_agent_hash = _hash_user_agent(request.headers.get("User-Agent"))
            ip_prefix = _extract_ip_prefix()
            allow_idempotent_replay = link_mode or _has_recent_session_for_same_client(
                db,
                user_id=user.id,
                user_agent_hash=user_agent_hash,
                ip_prefix=ip_prefix,
            )
            if not allow_idempotent_replay:
                return _auth_error_response("replay_detected", 401)

        if verified_identity:
            verified_identity.last_login_at = utcnow()
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

        sync_bootstrap_staff_assignment_for_user(db, user_id=user.id)
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
    link_mode_requested = _is_link_mode_requested(payload)
    current_session_user_id = getattr(g, "user_id", None)
    current_user_id = current_session_user_id if link_mode_requested else None
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
            identity.last_login_at = utcnow()
        channel = db.query(NotificationChannel).filter(
            NotificationChannel.channel_type == "vk",
            NotificationChannel.target_ref == vk_user_id,
        ).first()
        if not channel:
            channel = NotificationChannel(user_id=user.id, channel_type="vk", target_ref=vk_user_id, is_verified=False)
            db.add(channel)
        channel.is_enabled = True
        channel.is_verified = bool(channel.is_verified)
        channel.is_primary = False

        link_mode = bool(link_mode_requested and current_session_user_id and int(current_session_user_id) == int(user.id))
        sync_bootstrap_staff_assignment_for_user(db, user_id=user.id)
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
        login_user_id = int((merge_result or {}).get("primary_user_id") or user.id)
        sync_bootstrap_staff_assignment_for_user(db, user_id=login_user_id)
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
    purpose = str(payload.get("purpose") or "login").strip() or "login"
    if not phone:
        return _auth_error_response("phone_required", 400)
    provider = PhoneCodeAuthProvider()
    try:
        hit_rate_limit("otp_request", f"{_rate_limit_subject()}:{normalize_phone_e164(phone) or phone}")
        code = provider.request_code(db, phone=phone, purpose=purpose)
    except ValueError as exc:
        db.rollback()
        return _auth_error_response(str(exc), 400)
    except Exception as exc:
        handled = _handle_auth_provider_error(exc)
        if handled:
            db.rollback()
            return handled
        raise

    current_user_id = getattr(g, "user_id", None)
    delivery = send_phone_otp(
        db,
        phone=phone,
        code=code,
        purpose=purpose,
        ttl_minutes=10,
        current_user_id=int(current_user_id) if current_user_id is not None else None,
    )

    if delivery.get("ok"):
        provider.set_delivery_metadata(
            db,
            phone=phone,
            code=code,
            purpose=purpose,
            delivery_channel=str(delivery.get("channel_type") or "internal"),
            delivery_target=str(delivery.get("target_ref") or ""),
        )
        db.commit()
        response = {
            "ok": True,
            "delivery": str(delivery.get("channel_type") or "internal"),
            "expires_in_seconds": 600,
        }
        if ENV in {"dev", "test"}:
            response["debug_code"] = code
        return response

    error_code = str(delivery.get("error") or "otp_delivery_failed")
    if error_code == "phone_not_linked":
        db.rollback()
        return _auth_error_response(
            "phone_not_linked",
            404,
            message="Account for this phone was not found. Sign in once via Telegram or VK Mini App first.",
            action="use_mini_app",
            fallback_auth_methods=["telegram", "vk"],
        )

    if error_code == "verified_phone_conflict":
        db.rollback()
        return _auth_error_response(
            "verified_phone_conflict",
            409,
            message=MERGE_CONFLICT_NOTICE,
            action="contact_support",
            fallback_auth_methods=DEFAULT_FALLBACK_AUTH_METHODS,
            conflict_user_ids=delivery.get("conflict_user_ids") or [],
        )

    if error_code == "user_not_found":
        db.rollback()
        return _auth_error_response("auth_required", 401, fallback_auth_methods=["telegram", "vk"])

    if ENV in {"dev", "test"}:
        db.commit()
        return {
            "ok": True,
            "delivery": "internal",
            "warning": error_code,
            "expires_in_seconds": 600,
            "debug_code": code,
        }

    db.rollback()
    if error_code == "no_delivery_channel":
        return _auth_error_response(
            "otp_channel_not_available",
            400,
            message="No active Telegram or VK channel is connected to this account.",
            fallback_auth_methods=["telegram", "vk"],
        )
    return _auth_error_response(
        "otp_delivery_failed",
        502,
        message="Unable to deliver OTP code via Telegram or VK right now.",
        fallback_auth_methods=["telegram", "vk"],
    )


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
        sync_bootstrap_staff_assignment_for_user(db, user_id=login_user.id)
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
