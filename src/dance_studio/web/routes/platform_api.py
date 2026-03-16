from __future__ import annotations

from flask import Blueprint, g, request

from dance_studio.auth.services.account_merge import AccountMergeService
from dance_studio.db.models import (
    NotificationChannel,
    NotificationPreference,
    User,
    WebPushSubscription,
)
from dance_studio.notifications.services.notification_service import NotificationService

bp = Blueprint("platform_api_routes", __name__, url_prefix="/api")


@bp.route("/app/bootstrap", methods=["GET"])
def app_bootstrap():
    db = g.db
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {
            "user": None,
            "platform": "web",
            "auth_methods": ["telegram", "vk", "phone", "passkey"],
            "channels": [],
            "preferences": [],
            "feature_flags": {"passkey_scaffold": True},
        }

    user = db.query(User).filter(User.id == user_id).first()
    channels = db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id).all()
    prefs = db.query(NotificationPreference).filter(NotificationPreference.user_id == user_id).all()
    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "telegram_id": user.telegram_id,
            "primary_phone": user.primary_phone,
            "preferred_notification_channel": user.preferred_notification_channel,
        },
        "platform": request.args.get("platform", "web"),
        "auth_methods": ["telegram", "vk", "phone", "passkey"],
        "channels": [
            {
                "id": c.id,
                "channel_type": c.channel_type,
                "target_ref": c.target_ref,
                "is_primary": c.is_primary,
                "is_enabled": c.is_enabled,
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
        "feature_flags": {"passkey_scaffold": True},
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
    return {"items": [{"id": r.id, "channel_type": r.channel_type, "target_ref": r.target_ref, "is_primary": r.is_primary, "is_enabled": r.is_enabled} for r in rows]}


@bp.route("/notifications/channels/select-primary", methods=["POST"])
def select_primary_channel():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {"error": "auth required"}, 401
    payload = request.get_json(silent=True) or {}
    channel_id = int(payload.get("channel_id") or 0)
    channels = g.db.query(NotificationChannel).filter(NotificationChannel.user_id == user_id).all()
    found = False
    for channel in channels:
        channel.is_primary = channel.id == channel_id
        if channel.is_primary:
            found = True
    if not found:
        return {"error": "channel_not_found"}, 404
    g.db.commit()
    return {"ok": True}


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
