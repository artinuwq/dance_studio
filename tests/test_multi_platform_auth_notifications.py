from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.db.models import Base, SessionRecord, User
from dance_studio.web.app import create_app
from dance_studio.web.services.auth_session import _sid_hash


@pytest.fixture(scope="module")
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def app(session_factory, monkeypatch):
    def _get_session():
        return session_factory()

    monkeypatch.setattr(auth_middleware, "get_session", _get_session)
    monkeypatch.setattr(db_module, "get_session", _get_session)
    monkeypatch.setattr(auth_middleware, "_is_csrf_valid", lambda: True)
    monkeypatch.setattr("dance_studio.auth.providers.telegram.validate_init_data", lambda _: type("V", (), {"user_id": 777, "replay_key": "rk"})())
    monkeypatch.setattr("dance_studio.web.routes.auth.store_used_init_data", lambda *args, **kwargs: True)
    return create_app()


def _login_by_session(client, db, user_id: int, telegram_id: int | None = None):
    sid = secrets.token_hex(16)
    now = datetime.utcnow()
    db.add(
        SessionRecord(
            id=secrets.token_hex(32),
            telegram_id=telegram_id,
            user_id=user_id,
            sid_hash=_sid_hash(sid),
            last_seen=now,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    db.commit()
    client.set_cookie("localhost", "sid", sid)


def test_auth_vk_and_phone_flow(app, session_factory):
    client = app.test_client()

    vk_resp = client.post("/auth/vk", json={"vk_user_id": "12345", "name": "VK User"})
    assert vk_resp.status_code == 200

    request_code = client.post("/auth/phone/request-code", json={"phone": "+79990000000"})
    assert request_code.status_code == 200
    code = request_code.get_json()["debug_code"]

    verify = client.post("/auth/phone/verify-code", json={"phone": "+79990000000", "code": code})
    assert verify.status_code == 200


def test_notifications_preferences_and_web_push(app, session_factory):
    db = session_factory()
    user = User(name="Tester", telegram_id=9001)
    db.add(user)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    p = client.post("/api/notifications/preferences", json={"event_type": "lesson_reminder", "channel_type": "telegram", "priority": 1, "is_enabled": True})
    assert p.status_code == 200

    s = client.post("/api/notifications/web-push/subscribe", json={"endpoint": "https://push.example/1", "keys": {"p256dh": "k", "auth": "a"}})
    assert s.status_code == 200

    send = client.post("/api/notifications/test-send", json={"event_type": "lesson_reminder", "title": "A", "body": "B"})
    assert send.status_code == 200


def test_account_merge_preview_and_confirm(app, session_factory):
    db = session_factory()
    u1 = User(name="A", telegram_id=10001)
    u2 = User(name="B", telegram_id=10002)
    db.add_all([u1, u2])
    db.commit()

    client = app.test_client()
    preview = client.post("/api/account/merge/preview", json={"user_a_id": u1.id, "user_b_id": u2.id})
    assert preview.status_code == 200

    confirm = client.post("/api/account/merge/confirm", json={"user_a_id": u1.id, "user_b_id": u2.id, "reason": "test"})
    assert confirm.status_code == 200
