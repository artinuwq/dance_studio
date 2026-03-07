from __future__ import annotations

import json
from typing import Any

from dance_studio.db.models import NotificationDispatchLog


def _normalize_dispatch_ref(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("notification dispatch reference must not be empty")
    return text


def notification_dispatch_exists(
    db,
    *,
    notification_key: str,
    entity_type: str,
    entity_ref: Any,
    recipient_type: str = "telegram_user",
    recipient_ref: Any,
    statuses: set[str] | list[str] | tuple[str, ...] | None = None,
) -> bool:
    query = db.query(NotificationDispatchLog.id).filter_by(
            notification_key=_normalize_dispatch_ref(notification_key),
            entity_type=_normalize_dispatch_ref(entity_type),
            entity_ref=_normalize_dispatch_ref(entity_ref),
            recipient_type=_normalize_dispatch_ref(recipient_type),
            recipient_ref=_normalize_dispatch_ref(recipient_ref),
        )
    if statuses:
        normalized_statuses = [_normalize_dispatch_ref(status) for status in statuses]
        query = query.filter(NotificationDispatchLog.status.in_(normalized_statuses))
    return query.first() is not None


def record_notification_dispatch(
    db,
    *,
    notification_key: str,
    entity_type: str,
    entity_ref: Any,
    recipient_type: str = "telegram_user",
    recipient_ref: Any,
    status: str = "sent",
    payload: Any = None,
) -> NotificationDispatchLog:
    serialized_payload = payload
    if isinstance(payload, (dict, list, tuple)):
        serialized_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    row = NotificationDispatchLog(
        notification_key=_normalize_dispatch_ref(notification_key),
        entity_type=_normalize_dispatch_ref(entity_type),
        entity_ref=_normalize_dispatch_ref(entity_ref),
        recipient_type=_normalize_dispatch_ref(recipient_type),
        recipient_ref=_normalize_dispatch_ref(recipient_ref),
        status=_normalize_dispatch_ref(status),
        payload=serialized_payload,
    )
    db.add(row)
    db.flush()
    return row
