from __future__ import annotations

import json
from datetime import datetime

from dance_studio.db.models import AuthAuditEvent


def log_auth_event(
    db,
    *,
    event_type: str,
    provider: str | None = None,
    user_id: int | None = None,
    status: str = "ok",
    payload: dict | None = None,
) -> AuthAuditEvent:
    event = AuthAuditEvent(
        user_id=user_id,
        event_type=event_type,
        provider=provider,
        status=status,
        payload_json=(json.dumps(payload, ensure_ascii=False) if payload is not None else None),
        created_at=datetime.utcnow(),
    )
    db.add(event)
    return event
