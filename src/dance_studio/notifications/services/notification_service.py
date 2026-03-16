from __future__ import annotations

import json
from datetime import datetime

from dance_studio.db.models import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPreference,
)
from dance_studio.notifications.providers.telegram import TelegramNotificationProvider
from dance_studio.notifications.providers.vk import VkNotificationProvider
from dance_studio.notifications.providers.web_push import WebPushNotificationProvider


class NotificationService:
    def __init__(self):
        self.providers = {
            "telegram": TelegramNotificationProvider(),
            "vk": VkNotificationProvider(),
            "web_push": WebPushNotificationProvider(),
        }

    def _resolve_channels(self, db, user_id: int, event_type: str) -> list[NotificationChannel]:
        pref = (
            db.query(NotificationPreference)
            .filter(
                NotificationPreference.user_id == user_id,
                NotificationPreference.event_type == event_type,
                NotificationPreference.is_enabled.is_(True),
            )
            .order_by(NotificationPreference.priority.asc())
            .all()
        )
        if pref:
            types = [p.channel_type for p in pref]
            channels = (
                db.query(NotificationChannel)
                .filter(
                    NotificationChannel.user_id == user_id,
                    NotificationChannel.channel_type.in_(types),
                    NotificationChannel.is_enabled.is_(True),
                )
                .all()
            )
            by_type = {c.channel_type: c for c in channels}
            return [by_type[t] for t in types if t in by_type]

        return (
            db.query(NotificationChannel)
            .filter(NotificationChannel.user_id == user_id, NotificationChannel.is_enabled.is_(True))
            .order_by(NotificationChannel.is_primary.desc(), NotificationChannel.id.asc())
            .all()
        )

    def send(self, db, *, user_id: int, event_type: str, title: str, body: str, payload: dict | None = None) -> Notification:
        notification = Notification(
            user_id=user_id,
            event_type=event_type,
            title=title,
            body=body,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
            status="processing",
            processed_at=datetime.utcnow(),
        )
        db.add(notification)
        db.flush()

        channels = self._resolve_channels(db, user_id, event_type)
        sent_count = 0
        for channel in channels:
            provider = self.providers.get(channel.channel_type)
            if not provider:
                continue
            result = provider.send(channel.target_ref, title, body, payload)
            ok = bool(result.get("ok"))
            db.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    channel_type=channel.channel_type,
                    target_ref=channel.target_ref,
                    status="sent" if ok else "failed",
                    provider_message_id=result.get("provider_message_id"),
                    error_message=result.get("error"),
                    attempted_at=datetime.utcnow(),
                    delivered_at=datetime.utcnow() if ok else None,
                    payload_json=json.dumps(result, ensure_ascii=False),
                )
            )
            if ok:
                sent_count += 1
                break

        if sent_count > 0:
            notification.status = "sent"
        elif channels:
            notification.status = "failed"
        else:
            notification.status = "failed"
        return notification
