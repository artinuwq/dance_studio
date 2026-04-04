from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from dance_studio.db.models import AuthIdentity, Base, NotificationChannel, NotificationDelivery, NotificationPreference, User
from dance_studio.notifications.services.notification_service import NotificationService


@pytest.fixture(scope="module")
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db(engine):
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _create_user(db, name: str) -> User:
    user = User(name=name)
    db.add(user)
    db.commit()
    return user


def _create_channel(db, *, user_id: int, channel_type: str, target_ref: str, is_primary: bool = False):
    channel = NotificationChannel(
        user_id=user_id,
        channel_type=channel_type,
        target_ref=target_ref,
        is_enabled=True,
        is_verified=True,
        is_primary=is_primary,
    )
    db.add(channel)
    db.commit()
    return channel


def _create_preference(db, *, user_id: int, event_type: str, channel_type: str, priority: int, is_enabled: bool = True):
    row = NotificationPreference(
        user_id=user_id,
        event_type=event_type,
        channel_type=channel_type,
        priority=priority,
        is_enabled=is_enabled,
    )
    db.add(row)
    db.commit()
    return row


def test_wildcard_preferences_used_when_event_specific_missing(db):
    user = _create_user(db, "Wildcard user")
    _create_channel(db, user_id=user.id, channel_type="telegram", target_ref=f"tg-{user.id}")
    _create_channel(db, user_id=user.id, channel_type="vk", target_ref=f"vk-{user.id}")
    _create_preference(db, user_id=user.id, event_type="*", channel_type="vk", priority=1)
    _create_preference(db, user_id=user.id, event_type="*", channel_type="telegram", priority=2)

    service = NotificationService()
    channels = service._resolve_channels(db, user.id, "lesson_reminder")
    assert [channel.channel_type for channel in channels] == ["vk", "telegram"]


def test_event_specific_preferences_override_wildcard_order(db):
    user = _create_user(db, "Specific user")
    _create_channel(db, user_id=user.id, channel_type="telegram", target_ref=f"tg-{user.id}")
    _create_channel(db, user_id=user.id, channel_type="vk", target_ref=f"vk-{user.id}")
    _create_preference(db, user_id=user.id, event_type="*", channel_type="vk", priority=1)
    _create_preference(db, user_id=user.id, event_type="*", channel_type="telegram", priority=2)
    _create_preference(db, user_id=user.id, event_type="lesson_reminder", channel_type="telegram", priority=1)

    service = NotificationService()
    channels = service._resolve_channels(db, user.id, "lesson_reminder")
    assert [channel.channel_type for channel in channels] == ["telegram"]


def test_primary_channel_fallback_without_preferences(db):
    user = _create_user(db, "Primary fallback")
    _create_channel(db, user_id=user.id, channel_type="telegram", target_ref=f"tg-{user.id}", is_primary=False)
    _create_channel(db, user_id=user.id, channel_type="vk", target_ref=f"vk-{user.id}", is_primary=True)

    service = NotificationService()
    channels = service._resolve_channels(db, user.id, "any_event")
    assert [channel.channel_type for channel in channels][:2] == ["vk", "telegram"]


def test_vk_identity_does_not_auto_verify_channel(db):
    user = _create_user(db, "VK permission pending")
    db.add(AuthIdentity(user_id=user.id, provider="vk", provider_user_id=f"vk-{user.id}", is_verified=True))
    pending_vk = NotificationChannel(
        user_id=user.id,
        channel_type="vk",
        target_ref=f"vk-{user.id}",
        is_enabled=True,
        is_verified=False,
        is_primary=True,
    )
    db.add(pending_vk)
    db.commit()

    service = NotificationService()
    channels = service._resolve_channels(db, user.id, "any_event")

    assert [channel.channel_type for channel in channels] == []
    refreshed = db.query(NotificationChannel).filter(NotificationChannel.id == pending_vk.id).first()
    assert refreshed is not None
    assert refreshed.is_verified is False


def test_vk_permission_error_unverifies_channel_and_falls_back_to_next_channel(db):
    user = _create_user(db, "VK fallback after permission revoke")
    vk_channel = _create_channel(db, user_id=user.id, channel_type="vk", target_ref=f"{user.id}01", is_primary=True)
    tg_channel = _create_channel(db, user_id=user.id, channel_type="telegram", target_ref=f"{user.id}02", is_primary=False)

    service = NotificationService()
    service.providers["vk"] = type(
        "VkProviderStub",
        (),
        {"send": staticmethod(lambda *args, **kwargs: {"ok": False, "error": "vk_api_901:can't send messages to this user"})},
    )()
    service.providers["telegram"] = type(
        "TelegramProviderStub",
        (),
        {"send": staticmethod(lambda *args, **kwargs: {"ok": True, "provider_message_id": "tg:1"})},
    )()

    notification = service.send(
        db,
        user_id=user.id,
        event_type="legacy_user_notification",
        title="Payment details",
        body="Body",
    )
    db.flush()

    assert notification.status == "sent"
    refreshed_vk = db.query(NotificationChannel).filter(NotificationChannel.id == vk_channel.id).first()
    assert refreshed_vk is not None
    assert refreshed_vk.is_verified is False

    deliveries = (
        db.query(NotificationDelivery)
        .filter(NotificationDelivery.notification_id == notification.id)
        .order_by(NotificationDelivery.id.asc())
        .all()
    )
    assert [delivery.channel_type for delivery in deliveries] == ["vk", "telegram"]
    assert deliveries[0].status == "failed"
    assert deliveries[0].error_message == "vk_api_901:can't send messages to this user"
    assert deliveries[1].status == "sent"
    assert tg_channel.id > 0
