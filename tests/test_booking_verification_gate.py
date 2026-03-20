from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.db.models import Base, SessionRecord, User, UserPhone
from dance_studio.web.app import create_app
from dance_studio.web.services.auth_session import _sid_hash


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


@pytest.fixture
def app(session_factory, monkeypatch):
    def _get_session():
        return session_factory()

    monkeypatch.setattr(auth_middleware, "get_session", _get_session)
    monkeypatch.setattr(db_module, "get_session", _get_session)
    monkeypatch.setattr(auth_middleware, "_is_csrf_valid", lambda: True)
    return create_app()


def _seed_user_with_session(db, *, telegram_id: int, verified_phone: bool) -> str:
    user = User(name=f"User {telegram_id}", telegram_id=telegram_id)
    db.add(user)
    db.flush()
    if verified_phone:
        now = datetime.utcnow()
        db.add(
            UserPhone(
                user_id=user.id,
                phone_e164=f"+7999{telegram_id:07d}"[-12:],
                source="sms",
                verified_at=now,
                is_primary=True,
            )
        )
        user.phone = f"+7999{telegram_id:07d}"[-12:]
        user.primary_phone = user.phone
        user.phone_verified_at = now

    sid = secrets.token_hex(16)
    now = datetime.utcnow()
    db.add(
        SessionRecord(
            id=secrets.token_hex(32),
            telegram_id=telegram_id,
            user_id=user.id,
            user_agent_hash=None,
            sid_hash=_sid_hash(sid),
            ip_prefix=None,
            need_reauth=False,
            reauth_reason=None,
            last_seen=now,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    db.commit()
    return sid


def test_booking_requests_require_verified_phone(app, session_factory):
    db = session_factory()
    try:
        sid = _seed_user_with_session(db, telegram_id=3001, verified_phone=False)
    finally:
        db.close()

    client = app.test_client()
    client.set_cookie("sid", sid)
    response = client.post(
        "/api/booking-requests",
        json={
            "object_type": "rental",
            "date": "2099-01-01",
            "time_from": "10:00",
            "time_to": "11:00",
            "comment": "Проверка gate",
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "phone_verification_required",
        "message": "Ваш аккаунт не подтверждён",
        "action": "verify_phone",
    }


def test_booking_requests_allow_verified_phone_users(app, session_factory):
    db = session_factory()
    try:
        sid = _seed_user_with_session(db, telegram_id=3002, verified_phone=True)
    finally:
        db.close()

    client = app.test_client()
    client.set_cookie("sid", sid)
    response = client.post(
        "/api/booking-requests",
        json={
            "object_type": "rental",
            "date": "2099-01-01",
            "time_from": "10:00",
            "time_to": "11:00",
            "comment": "Проверка gate",
        },
    )

    payload = response.get_json()
    assert response.status_code == 201, payload
    assert payload["status"] == "created"
