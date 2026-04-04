from __future__ import annotations

import secrets
from dataclasses import dataclass

import requests

from dance_studio.auth.services.common import get_verified_phone_user, normalize_phone_e164
from dance_studio.core.config import BOT_TOKEN, VK_API_VERSION, VK_COMMUNITY_ACCESS_TOKEN
from dance_studio.db.models import AuthIdentity, NotificationChannel, NotificationPreference, User


OTP_EVENT_TYPE = "auth_otp"
SUPPORTED_CHANNEL_TYPES = ("telegram", "vk")


@dataclass(frozen=True)
class DeliveryTarget:
    channel_type: str
    target_ref: str


def _resolve_preferred_channel_types(db, *, user_id: int) -> list[str]:
    for event_type in (OTP_EVENT_TYPE, "*"):
        rows = (
            db.query(NotificationPreference)
            .filter(
                NotificationPreference.user_id == user_id,
                NotificationPreference.event_type == event_type,
                NotificationPreference.is_enabled.is_(True),
            )
            .order_by(NotificationPreference.priority.asc(), NotificationPreference.id.asc())
            .all()
        )
        if not rows:
            continue
        result: list[str] = []
        seen: set[str] = set()
        for row in rows:
            channel_type = str(row.channel_type or "").strip()
            if channel_type not in SUPPORTED_CHANNEL_TYPES or channel_type in seen:
                continue
            seen.add(channel_type)
            result.append(channel_type)
        if result:
            return result
    return []


def _resolve_targets(db, *, user: User) -> list[DeliveryTarget]:
    rows = (
        db.query(NotificationChannel)
        .filter(
            NotificationChannel.user_id == user.id,
            NotificationChannel.channel_type.in_(SUPPORTED_CHANNEL_TYPES),
            NotificationChannel.is_enabled.is_(True),
        )
        .order_by(NotificationChannel.is_primary.desc(), NotificationChannel.id.asc())
        .all()
    )

    by_type: dict[str, list[NotificationChannel]] = {"telegram": [], "vk": []}
    for row in rows:
        if row.channel_type in by_type and str(row.target_ref or "").strip():
            by_type[row.channel_type].append(row)

    preferred_types = _resolve_preferred_channel_types(db, user_id=int(user.id))
    ordered: list[DeliveryTarget] = []
    seen: set[tuple[str, str]] = set()

    def _append(channel_type: str, target_ref: str) -> None:
        key = (channel_type, target_ref)
        if key in seen:
            return
        seen.add(key)
        ordered.append(DeliveryTarget(channel_type=channel_type, target_ref=target_ref))

    for channel_type in preferred_types:
        for row in by_type.get(channel_type, []):
            _append(channel_type, str(row.target_ref).strip())

    for row in rows:
        target_ref = str(row.target_ref or "").strip()
        if not target_ref:
            continue
        _append(str(row.channel_type).strip(), target_ref)

    if user.telegram_id:
        _append("telegram", str(user.telegram_id))

    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.user_id == user.id,
            AuthIdentity.provider == "vk",
            AuthIdentity.provider_user_id.isnot(None),
        )
        .order_by(AuthIdentity.last_login_at.desc(), AuthIdentity.id.desc())
        .first()
    )
    if identity and identity.provider_user_id:
        _append("vk", str(identity.provider_user_id).strip())

    return ordered


def _build_otp_text(*, code: str, ttl_minutes: int, purpose: str) -> str:
    flow = "login" if purpose == "login" else purpose
    return (
        f"Verification code: {code}\n"
        f"Flow: {flow}\n"
        f"Expires in {ttl_minutes} minutes.\n"
        "If you did not request this code, ignore this message."
    )


def _send_telegram(target_ref: str, text: str) -> tuple[bool, str | None, str | None]:
    if not BOT_TOKEN:
        return False, None, "telegram_not_configured"
    try:
        chat_id = int(str(target_ref).strip())
    except (TypeError, ValueError):
        return False, None, "invalid_telegram_target"

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
            },
            timeout=10,
        )
    except Exception as exc:
        return False, None, f"telegram_exception:{exc}"

    if not response.ok:
        try:
            payload = response.json() if response.content else {}
        except Exception:
            payload = {}
        description = str((payload or {}).get("description") or "").strip()
        return False, None, f"telegram_http_{response.status_code}:{description or 'send_failed'}"

    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    if not bool((payload or {}).get("ok")):
        description = str((payload or {}).get("description") or "").strip()
        return False, None, f"telegram_api:{description or 'send_failed'}"

    message_id = ((payload or {}).get("result") or {}).get("message_id")
    provider_message_id = f"tg:{message_id}" if message_id is not None else None
    return True, provider_message_id, None


def _send_vk(target_ref: str, text: str) -> tuple[bool, str | None, str | None]:
    if not VK_COMMUNITY_ACCESS_TOKEN:
        return False, None, "vk_not_configured"
    try:
        user_id = int(str(target_ref).strip())
    except (TypeError, ValueError):
        return False, None, "invalid_vk_target"
    if user_id <= 0:
        return False, None, "invalid_vk_target"

    payload = {
        "access_token": VK_COMMUNITY_ACCESS_TOKEN,
        "v": VK_API_VERSION or "5.199",
        "user_id": user_id,
        "random_id": secrets.randbelow(2_147_483_647),
        "message": text,
    }

    try:
        response = requests.post("https://api.vk.com/method/messages.send", data=payload, timeout=10)
    except Exception as exc:
        return False, None, f"vk_exception:{exc}"

    if not response.ok:
        return False, None, f"vk_http_{response.status_code}:send_failed"

    try:
        data = response.json() if response.content else {}
    except Exception:
        data = {}

    if isinstance(data, dict) and "response" in data:
        provider_message_id = f"vk:{data.get('response')}"
        return True, provider_message_id, None

    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        error_code = error.get("error_code")
        error_msg = str(error.get("error_msg") or "send_failed").strip()
        return False, None, f"vk_api_{error_code}:{error_msg}"

    return False, None, "vk_send_failed"


def send_phone_otp(
    db,
    *,
    phone: str,
    code: str,
    purpose: str = "login",
    ttl_minutes: int = 10,
    current_user_id: int | None = None,
) -> dict:
    normalized_phone = normalize_phone_e164(phone)
    if not normalized_phone:
        return {"ok": False, "error": "invalid_phone"}

    user = None
    if current_user_id is not None:
        user = db.query(User).filter(User.id == int(current_user_id), User.is_archived.is_(False)).first()
        if not user:
            return {"ok": False, "error": "user_not_found"}
    else:
        user, matched_user_ids = get_verified_phone_user(db, phone_e164=normalized_phone)
        if len(matched_user_ids) > 1:
            return {"ok": False, "error": "verified_phone_conflict", "conflict_user_ids": matched_user_ids}
        if not user:
            return {"ok": False, "error": "phone_not_linked"}

    targets = _resolve_targets(db, user=user)
    if not targets:
        return {"ok": False, "error": "no_delivery_channel", "user_id": user.id}

    text = _build_otp_text(code=code, ttl_minutes=ttl_minutes, purpose=purpose)
    attempts: list[dict] = []
    for target in targets:
        if target.channel_type == "telegram":
            ok, provider_message_id, error = _send_telegram(target.target_ref, text)
        elif target.channel_type == "vk":
            ok, provider_message_id, error = _send_vk(target.target_ref, text)
        else:
            continue

        attempts.append(
            {
                "channel_type": target.channel_type,
                "target_ref": target.target_ref,
                "ok": bool(ok),
                "error": error,
            }
        )
        if ok:
            return {
                "ok": True,
                "user_id": user.id,
                "channel_type": target.channel_type,
                "target_ref": target.target_ref,
                "provider_message_id": provider_message_id,
                "attempts": attempts,
            }

    return {
        "ok": False,
        "error": "delivery_failed",
        "user_id": user.id,
        "attempts": attempts,
    }
