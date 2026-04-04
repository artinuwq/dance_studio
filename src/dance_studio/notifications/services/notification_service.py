from __future__ import annotations

import json
from datetime import datetime

from dance_studio.db.models import (
    AuthIdentity,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPreference,
    User,
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

    @staticmethod
    def _is_vk_channel_allowed(channel: NotificationChannel) -> bool:
        if str(channel.channel_type or "").strip() != "vk":
            return True
        return bool(channel.is_verified)

    def _ensure_legacy_channels(self, db, user: User | None) -> None:
        if not user:
            return

        def _ensure_channel(
            channel_type: str,
            target_ref: str | None,
            *,
            is_verified: bool,
            is_primary: bool = False,
            promote_existing_verification: bool = True,
        ) -> None:
            ref = str(target_ref or "").strip()
            if not ref:
                return
            channel = (
                db.query(NotificationChannel)
                .filter(
                    NotificationChannel.channel_type == channel_type,
                    NotificationChannel.target_ref == ref,
                )
                .first()
            )
            if channel and int(channel.user_id) != int(user.id):
                # Target is already attached to another user (usually stale data).
                # Do not rebind automatically during notification send.
                return
            if not channel:
                channel = NotificationChannel(
                    user_id=user.id,
                    channel_type=channel_type,
                    target_ref=ref,
                    is_enabled=True,
                    is_verified=is_verified,
                    is_primary=is_primary,
                )
                db.add(channel)
                return
            if is_verified and promote_existing_verification:
                channel.is_verified = True

        if user.telegram_id:
            _ensure_channel(
                "telegram",
                str(user.telegram_id),
                is_verified=True,
                is_primary=False,
            )

        identities = (
            db.query(AuthIdentity)
            .filter(
                AuthIdentity.user_id == user.id,
                AuthIdentity.provider.in_(["telegram", "vk"]),
                AuthIdentity.provider_user_id.isnot(None),
            )
            .order_by(AuthIdentity.id.desc())
            .all()
        )
        for identity in identities:
            provider = str(identity.provider or "").strip()
            provider_user_id = str(identity.provider_user_id or "").strip()
            if not provider or not provider_user_id:
                continue
            if provider == "telegram":
                _ensure_channel(
                    "telegram",
                    provider_user_id,
                    is_verified=True,
                    is_primary=False,
                )
            elif provider == "vk":
                _ensure_channel(
                    "vk",
                    provider_user_id,
                    # VK auth identity does not mean the user allowed messages
                    # from the community. This flag is granted explicitly via
                    # VK Mini App permission flow and stored on the channel.
                    is_verified=False,
                    is_primary=False,
                    promote_existing_verification=False,
                )

    @staticmethod
    def _vk_channel_requires_permission_refresh(result: dict | None) -> bool:
        error = str((result or {}).get("error") or "").strip().lower()
        return error.startswith("vk_api_901:") or error.startswith("vk_api_902:")

    def _apply_failed_delivery_side_effects(
        self,
        *,
        channel: NotificationChannel,
        result: dict | None,
    ) -> None:
        if str(channel.channel_type or "").strip() != "vk":
            return
        if not self._vk_channel_requires_permission_refresh(result):
            return
        channel.is_verified = False
        if isinstance(result, dict):
            result["requires_permission_refresh"] = True

    def _resolve_user(self, db, user_id: int) -> User | None:
        return db.query(User).filter(User.id == user_id, User.is_archived.is_(False)).first()

    def _resolve_channels(self, db, user_id: int, event_type: str) -> list[NotificationChannel]:
        user = self._resolve_user(db, user_id)
        self._ensure_legacy_channels(db, user)

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
        if not pref:
            pref = (
                db.query(NotificationPreference)
                .filter(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.event_type == "*",
                    NotificationPreference.is_enabled.is_(True),
                )
                .order_by(NotificationPreference.priority.asc())
                .all()
            )
        if pref:
            types: list[str] = []
            seen_types: set[str] = set()
            for row in pref:
                channel_type = str(row.channel_type or "").strip()
                if not channel_type or channel_type in seen_types:
                    continue
                seen_types.add(channel_type)
                types.append(channel_type)
            channels = (
                db.query(NotificationChannel)
                .filter(
                    NotificationChannel.user_id == user_id,
                    NotificationChannel.channel_type.in_(types),
                    NotificationChannel.is_enabled.is_(True),
                )
                .all()
            )
            by_type: dict[str, NotificationChannel] = {}
            for channel in channels:
                by_type.setdefault(channel.channel_type, channel)
            return [by_type[t] for t in types if t in by_type and self._is_vk_channel_allowed(by_type[t])]

        channels = (
            db.query(NotificationChannel)
            .filter(NotificationChannel.user_id == user_id, NotificationChannel.is_enabled.is_(True))
            .order_by(NotificationChannel.is_primary.desc(), NotificationChannel.id.asc())
            .all()
        )
        return [channel for channel in channels if self._is_vk_channel_allowed(channel)]

    def send(self, db, *, user_id: int, event_type: str, title: str, body: str, payload: dict | None = None) -> Notification:
        payload = payload or {}
        notification = Notification(
            user_id=user_id,
            event_type=event_type,
            title=title,
            body=body,
            payload_json=json.dumps(payload, ensure_ascii=False),
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
            if not ok:
                self._apply_failed_delivery_side_effects(channel=channel, result=result)
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
            notification.status = "no_channels"
        return notification
