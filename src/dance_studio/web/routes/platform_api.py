from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime

from flask import Blueprint, g, request

from dance_studio.auth.services.account_merge import AccountMergeService
from dance_studio.auth.services.bootstrap import AVAILABLE_AUTH_METHODS, auth_feature_flags, build_user_auth_contract
from dance_studio.core.config import APP_SECRET_KEY, VK_COMMUNITY_ID
from dance_studio.web.services.access import _get_current_staff, require_permission
from dance_studio.db.models import (
    NotificationChannel,
    NotificationPreference,
    Staff,
    User,
    UserMergeEvent,
    WebPushSubscription,
)
from dance_studio.notifications.services.notification_service import NotificationService

bp = Blueprint("platform_api_routes", __name__, url_prefix="/api")
VK_PERMISSION_KEY_TTL_SECONDS = 15 * 60


def _build_bootstrap_staff_payload(db, user: User | None) -> dict:
    if not user:
        return {"is_staff": False, "staff": None}

    staff = db.query(Staff).filter(Staff.user_id == user.id, Staff.status == "active").first()
    if not staff and user.telegram_id is not None:
        staff = db.query(Staff).filter(Staff.telegram_id == user.telegram_id, Staff.status == "active").first()
    if not staff:
        return {"is_staff": False, "staff": None}

    return {
        "is_staff": True,
        "staff": {
            "id": staff.id,
            "name": staff.name or user.name,
            "position": staff.position,
            "specialization": staff.specialization,
            "bio": staff.bio,
            "teaches": staff.teaches,
            "phone": staff.phone,
            "email": staff.email,
            "photo_path": staff.photo_path or user.photo_path,
        },
    }


def _serialize_merge_case(db, event: UserMergeEvent) -> dict:
    reviewer = db.query(Staff).filter(Staff.id == event.reviewed_by).first() if event.reviewed_by else None
    return {
        "id": event.id,
        "source_user_id": event.source_user_id,
        "target_user_id": event.target_user_id,
        "merge_reason": event.merge_reason,
        "merge_strategy": event.merge_strategy,
        "case_status": event.case_status,
        "conflict_source": event.conflict_source,
        "reviewed_by": event.reviewed_by,
        "reviewed_by_name": reviewer.name if reviewer else None,
        "reviewed_at": event.reviewed_at.isoformat() if event.reviewed_at else None,
        "review_result": event.review_result,
        "resolved_at": event.resolved_at.isoformat() if event.resolved_at else None,
        "payload_json": event.payload_json,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _serialize_notification_channel(channel: NotificationChannel) -> dict:
    return {
        "id": channel.id,
        "channel_type": channel.channel_type,
        "target_ref": channel.target_ref,
        "is_primary": channel.is_primary,
        "is_enabled": channel.is_enabled,
        "is_verified": channel.is_verified,
    }


def _find_vk_channel_for_user(db, *, user_id: int, channel_id: int = 0, target_ref: str = "") -> NotificationChannel | None:
    query = db.query(NotificationChannel).filter(
        NotificationChannel.user_id == int(user_id),
        NotificationChannel.channel_type == "vk",
    )
    if channel_id > 0:
        query = query.filter(NotificationChannel.id == int(channel_id))
    elif target_ref:
        query = query.filter(NotificationChannel.target_ref == target_ref)
    return query.order_by(NotificationChannel.is_primary.desc(), NotificationChannel.id.asc()).first()


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    normalized = str(value or "").strip()
    padding = "=" * (-len(normalized) % 4)
    return base64.urlsafe_b64decode(f"{normalized}{padding}")


def _sign_vk_permission_payload(payload_json: str) -> str:
    digest = hmac.new(APP_SECRET_KEY.encode("utf-8"), payload_json.encode("utf-8"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def _issue_vk_permission_key(user_id: int, channel: NotificationChannel) -> str:
    payload = {
        "purpose": "vk_allow_messages_from_group",
        "user_id": int(user_id),
        "channel_id": int(channel.id),
        "target_ref": str(channel.target_ref or ""),
        "issued_at": int(datetime.utcnow().timestamp()),
        "nonce": secrets.token_urlsafe(12),
    }
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    signature = _sign_vk_permission_payload(payload_json)
    return f"{_urlsafe_b64encode(payload_json.encode('utf-8'))}.{signature}"


def _resolve_vk_permission_group_id() -> int | None:
    raw_value = str(VK_COMMUNITY_ID or "").strip()
    if not raw_value:
        return None
    try:
        group_id = int(raw_value)
    except (TypeError, ValueError):
        return None
    return group_id if group_id > 0 else None


def _is_vk_permission_key_valid(permission_key: str, *, user_id: int, channel: NotificationChannel) -> bool:
    token = str(permission_key or "").strip()
    if not token or "." not in token:
        return False
    payload_part, signature = token.split(".", 1)
    if not payload_part or not signature:
        return False
    try:
        payload_json = _urlsafe_b64decode(payload_part).decode("utf-8")
        payload = json.loads(payload_json)
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    if not hmac.compare_digest(_sign_vk_permission_payload(payload_json), signature):
        return False
    if str(payload.get("purpose") or "") != "vk_allow_messages_from_group":
        return False
    try:
        issued_at = int(payload.get("issued_at") or 0)
    except (TypeError, ValueError):
        return False
    if issued_at <= 0:
        return False
    age_seconds = int(datetime.utcnow().timestamp()) - issued_at
    if age_seconds < 0 or age_seconds > VK_PERMISSION_KEY_TTL_SECONDS:
        return False
    if int(payload.get("user_id") or 0) != int(user_id):
        return False
    if int(payload.get("channel_id") or 0) != int(channel.id):
        return False
    if str(payload.get("target_ref") or "").strip() != str(channel.target_ref or "").strip():
        return False
    return bool(str(payload.get("nonce") or "").strip())


@bp.route("/app/bootstrap", methods=["GET"])
def app_bootstrap():
    db = g.db
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {
            "session": {"authenticated": False},
            "user": None,
            "staff": {"is_staff": False, "staff": None},
            "platform": request.args.get("platform", "web"),
            "auth_methods": AVAILABLE_AUTH_METHODS,
            "fallback_auth_methods": AVAILABLE_AUTH_METHODS,
            "channels": [],
            "preferences": [],
            "feature_flags": auth_feature_flags(),
        }

    user = db.query(User).filter(User.id == user_id).first()
    channels = db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id).all()
    prefs = db.query(NotificationPreference).filter(NotificationPreference.user_id == user_id).all()
    return {
        "session": {"authenticated": True},
        "user": build_user_auth_contract(db, user),
        "staff": _build_bootstrap_staff_payload(db, user),
        "platform": request.args.get("platform", "web"),
        "auth_methods": AVAILABLE_AUTH_METHODS,
        "fallback_auth_methods": [],
        "channels": [
            {
                "id": c.id,
                "channel_type": c.channel_type,
                "target_ref": c.target_ref,
                "is_primary": c.is_primary,
                "is_enabled": c.is_enabled,
                "is_verified": c.is_verified,
            }
            for c in channels
        ],
        "preferences": [
            {
                "id": p.id,
                "event_type": p.event_type,
                "channel_type": p.channel_type,
                "priority": p.priority,
                "is_enabled": p.is_enabled,
            }
            for p in prefs
        ],
        "feature_flags": auth_feature_flags(),
    }


@bp.route("/notifications/preferences", methods=["GET"])
def get_preferences():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    rows = g.db.query(NotificationPreference).filter(NotificationPreference.user_id == user_id).all()
    return {"items": [{"id": r.id, "event_type": r.event_type, "channel_type": r.channel_type, "priority": r.priority, "is_enabled": r.is_enabled} for r in rows]}


@bp.route("/notifications/preferences", methods=["POST"])
def upsert_preference():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get("event_type") or "").strip()
    channel_type = str(payload.get("channel_type") or "").strip()
    if not event_type or not channel_type:
        return {"error": "event_type_and_channel_type_required"}, 400
    row = (
        g.db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == user_id, NotificationPreference.event_type == event_type, NotificationPreference.channel_type == channel_type)
        .first()
    )
    if not row:
        row = NotificationPreference(user_id=user_id, event_type=event_type, channel_type=channel_type)
        g.db.add(row)
    row.priority = int(payload.get("priority") or row.priority or 100)
    row.is_enabled = bool(payload.get("is_enabled", True))
    g.db.commit()
    return {"ok": True, "id": row.id}


@bp.route("/notifications/channels", methods=["GET"])
def get_channels():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    rows = g.db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id).all()
    return {"items": [{"id": r.id, "channel_type": r.channel_type, "target_ref": r.target_ref, "is_primary": r.is_primary, "is_enabled": r.is_enabled, "is_verified": r.is_verified} for r in rows]}


@bp.route("/notifications/channels/select-primary", methods=["POST"])
def select_primary_channel():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    channel_id = int(payload.get("channel_id") or 0)
    channels = g.db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id).all()
    selected = next((channel for channel in channels if channel.id == channel_id), None)
    if not selected:
        return {"error": "channel_not_found"}, 404
    if selected.channel_type == "vk" and not bool(selected.is_verified):
        return {"error": "vk_permission_required"}, 409

    found = False
    for channel in channels:
        channel.is_primary = channel.id == channel_id
        if channel.is_primary:
            found = True
    if not found:
        return {"error": "channel_not_found"}, 404
    g.db.commit()
    return {"ok": True}


@bp.route("/notifications/channels/vk/request-permission", methods=["POST"])
def request_vk_channel_permission():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    channel_id = int(payload.get("channel_id") or 0)
    target_ref = str(payload.get("target_ref") or "").strip()
    channel = _find_vk_channel_for_user(g.db, user_id=int(user_id), channel_id=channel_id, target_ref=target_ref)
    if not channel:
        return {"error": "vk_channel_not_found"}, 404
    return {
        "ok": True,
        "permission_key": _issue_vk_permission_key(int(user_id), channel),
        "expires_in": VK_PERMISSION_KEY_TTL_SECONDS,
        "group_id": _resolve_vk_permission_group_id(),
        "channel": _serialize_notification_channel(channel),
    }


@bp.route("/notifications/channels/vk/mark-verified", methods=["POST"])
def mark_vk_channel_verified():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    channel_id = int(payload.get("channel_id") or 0)
    target_ref = str(payload.get("target_ref") or "").strip()
    permission_key = str(payload.get("permission_key") or "").strip()
    channel = _find_vk_channel_for_user(g.db, user_id=int(user_id), channel_id=channel_id, target_ref=target_ref)
    if not channel:
        return {"error": "vk_channel_not_found"}, 404
    if not permission_key:
        return {"error": "vk_permission_key_required"}, 400
    if not _is_vk_permission_key_valid(permission_key, user_id=int(user_id), channel=channel):
        return {"error": "vk_permission_key_invalid"}, 409

    channel.is_verified = True
    channel.is_enabled = True
    g.db.commit()
    return {
        "ok": True,
        "channel": _serialize_notification_channel(channel),
    }


@bp.route("/notifications/web-push/subscribe", methods=["POST"])
def subscribe_web_push():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint") or "").strip()
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return {"error": "invalid_subscription"}, 400

    row = g.db.query(WebPushSubscription).filter(WebPushSubscription.endpoint == endpoint).first()
    if not row:
        row = WebPushSubscription(user_id=user_id, endpoint=endpoint, p256dh=keys.get("p256dh"), auth=keys.get("auth"))
        g.db.add(row)
    row.user_id = user_id
    row.user_agent = request.headers.get("User-Agent")
    row.is_active = True

    channel = g.db.query(NotificationChannel).filter(NotificationChannel.channel_type == "web_push", NotificationChannel.target_ref == endpoint).first()
    if not channel:
        g.db.add(NotificationChannel(user_id=user_id, channel_type="web_push", target_ref=endpoint, is_verified=True, is_enabled=True, is_primary=False))

    g.db.commit()
    return {"ok": True}


@bp.route("/notifications/web-push/unsubscribe", methods=["POST"])
def unsubscribe_web_push():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint") or "").strip()
    row = g.db.query(WebPushSubscription).filter(WebPushSubscription.user_id == user_id, WebPushSubscription.endpoint == endpoint).first()
    if row:
        row.is_active = False
    channel = g.db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id, NotificationChannel.channel_type == "web_push", NotificationChannel.target_ref == endpoint).first()
    if channel:
        channel.is_enabled = False
    g.db.commit()
    return {"ok": True}


@bp.route("/account/merge/preview", methods=["POST"])
def account_merge_preview():
    payload = request.get_json(silent=True) or {}
    user_a_id = int(payload.get("user_a_id") or 0)
    user_b_id = int(payload.get("user_b_id") or 0)
    if not user_a_id or not user_b_id:
        return {"error": "user_a_id_and_user_b_id_required"}, 400
    svc = AccountMergeService()
    primary_id, secondary_id = svc.choose_primary_user(g.db, user_a_id, user_b_id)
    return {"primary_user_id": primary_id, "secondary_user_id": secondary_id}


@bp.route("/account/merge/confirm", methods=["POST"])
def account_merge_confirm():
    payload = request.get_json(silent=True) or {}
    user_a_id = int(payload.get("user_a_id") or 0)
    user_b_id = int(payload.get("user_b_id") or 0)
    reason = str(payload.get("reason") or "manual")
    if not user_a_id or not user_b_id:
        return {"error": "user_a_id_and_user_b_id_required"}, 400
    svc = AccountMergeService()
    try:
        primary_id, secondary_id = svc.merge_users(g.db, user_a_id=user_a_id, user_b_id=user_b_id, reason=reason)
        g.db.commit()
    except Exception:
        g.db.rollback()
        raise
    return {"ok": True, "primary_user_id": primary_id, "secondary_user_id": secondary_id}


@bp.route("/admin/manual-merge-cases", methods=["GET"])
def list_manual_merge_cases():
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    svc = AccountMergeService()
    return {"items": [_serialize_merge_case(g.db, event) for event in svc.list_pending_merge_cases(g.db)]}


@bp.route("/admin/manual-merge-cases/<int:event_id>", methods=["GET"])
def get_manual_merge_case(event_id: int):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    svc = AccountMergeService()
    event = svc.get_merge_case(g.db, event_id=event_id)
    if not event:
        return {"error": "merge_case_not_found"}, 404
    return _serialize_merge_case(g.db, event)


@bp.route("/admin/manual-merge-cases/<int:event_id>/review", methods=["POST"])
def review_manual_merge_case(event_id: int):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    payload = request.get_json(silent=True) or {}
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject", "ignore"}:
        return {"error": "decision_must_be_approve_reject_or_ignore"}, 400

    reviewer_id = None
    current_staff = _get_current_staff(g.db)
    if current_staff and getattr(current_staff, "id", None):
        reviewer_id = int(current_staff.id)
    if reviewer_id is None:
        return {"error": "reviewer_not_found"}, 403

    svc = AccountMergeService()
    try:
        event = svc.review_merge_case(
            g.db,
            event_id=event_id,
            decision=decision,
            reviewed_by=reviewer_id,
            reason=str(payload.get("reason") or "").strip() or None,
        )
    except ValueError as exc:
        g.db.rollback()
        return {"error": str(exc)}, 400
    if not event:
        g.db.rollback()
        return {"error": "merge_case_not_found"}, 404
    g.db.commit()
    return {"ok": True, "case": _serialize_merge_case(g.db, event)}


@bp.route("/account/link/request", methods=["POST"])
def account_link_request():
    return {"ok": True, "message": "Link flow is available via phone code endpoints"}


@bp.route("/account/link/confirm", methods=["POST"])
def account_link_confirm():
    return {"ok": True, "message": "Link flow scaffolded"}


@bp.route("/notifications/test-send", methods=["POST"])
def test_send_notification():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    service = NotificationService()
    notification = service.send(
        g.db,
        user_id=user_id,
        event_type=str(payload.get("event_type") or "news_published"),
        title=str(payload.get("title") or "Тест"),
        body=str(payload.get("body") or "Тестовое уведомление"),
        payload=payload.get("payload") or {},
    )
    g.db.commit()
    return {"ok": True, "notification_id": notification.id, "status": notification.status}
