from __future__ import annotations

from flask import Blueprint, g, request

from dance_studio.auth.services.account_merge import AccountMergeService
from dance_studio.auth.services.bootstrap import AVAILABLE_AUTH_METHODS, auth_feature_flags, build_user_auth_contract
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


@bp.route("/app/bootstrap", methods=["GET"])
def app_bootstrap():
    db = g.db
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return {
            "session": {"authenticated": False},
            "user": None,
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
