from __future__ import annotations

import os

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from dance_studio.db.models import AuthIdentity, Base, User
from dance_studio.web.app import create_app

create_app()

from dance_studio.core import notification_service as bridge
from dance_studio.web.services import bookings as bookings_service


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_send_user_notification_uses_resolved_telegram_id_for_direct_fallback(session_factory, monkeypatch):
    db = session_factory()
    user = User(name="VK fallback user")
    db.add(user)
    db.flush()
    user_id = user.id
    db.add(
        AuthIdentity(
            user_id=user_id,
            provider="telegram",
            provider_user_id="777001",
            is_verified=True,
        )
    )
    db.commit()
    db.close()

    attempted_targets: list[int] = []

    monkeypatch.setattr(bridge, "get_session", lambda: session_factory())
    monkeypatch.setattr(bridge, "_send_via_multichannel", lambda *args, **kwargs: False)
    monkeypatch.setattr(bridge, "resolve_tech_logs_chat_id", lambda: None)
    monkeypatch.setattr(bridge, "_ensure_forum_topic", lambda *args, **kwargs: None)

    def _fake_direct_send(target_ref, text, payload):
        attempted_targets.append(int(target_ref))
        return True, None

    monkeypatch.setattr(bridge, "_send_direct_telegram", _fake_direct_send)

    sent = bridge.send_user_notification_sync(
        user_id=user_id,
        text="Test payment details",
        context_note="Booking payment details",
    )

    assert sent is True
    assert attempted_targets == [777001]


def test_send_user_notification_does_not_fallback_to_internal_user_id(session_factory, monkeypatch):
    db = session_factory()
    user = User(name="No internal id fallback")
    db.add(user)
    db.flush()
    user_id = user.id
    db.add(
        AuthIdentity(
            user_id=user_id,
            provider="telegram",
            provider_user_id="888002",
            is_verified=True,
        )
    )
    db.commit()
    db.close()

    attempted_targets: list[int] = []

    monkeypatch.setattr(bridge, "get_session", lambda: session_factory())
    monkeypatch.setattr(bridge, "_send_via_multichannel", lambda *args, **kwargs: False)
    monkeypatch.setattr(bridge, "resolve_tech_logs_chat_id", lambda: None)
    monkeypatch.setattr(bridge, "_ensure_forum_topic", lambda *args, **kwargs: None)

    def _fake_direct_send(target_ref, text, payload):
        attempted_targets.append(int(target_ref))
        return False, "telegram_http_403:chat not found"

    monkeypatch.setattr(bridge, "_send_direct_telegram", _fake_direct_send)

    sent = bridge.send_user_notification_sync(
        user_id=user_id,
        text="Test payment details",
        context_note="Booking payment details",
    )

    assert sent is False
    assert attempted_targets == [888002]


def test_enqueue_booking_payment_details_delivery_starts_background_thread(monkeypatch):
    started: dict[str, object] = {}

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            started["target"] = target
            started["args"] = args
            started["name"] = name
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(bookings_service, "Thread", FakeThread)

    app = Flask(__name__)
    with app.app_context():
        bookings_service.enqueue_booking_payment_details_delivery(65, 12)

    assert started["target"] is bookings_service._deliver_booking_payment_details_in_background
    assert started["name"] == "booking-payment-65"
    assert started["daemon"] is True
    assert started["args"][1:] == (65, 12)
    assert started["started"] is True
