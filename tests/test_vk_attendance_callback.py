from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from dance_studio.core.statuses import ABONEMENT_STATUS_ACTIVE
from dance_studio.db.models import (
    AttendanceIntention,
    AttendanceReminder,
    Base,
    Direction,
    Group,
    GroupAbonement,
    NotificationChannel,
    Schedule,
    Staff,
    User,
)
from dance_studio.web.app import create_app
from dance_studio.web.routes import platform_api


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
    return create_app()


def _seed_group_schedule_with_vk_channel(session_factory, *, vk_user_id: int = 551122) -> tuple[int, int]:
    db = session_factory()
    try:
        teacher = Staff(name="Teacher", position="teacher", status="active")
        direction = Direction(title="Dance", direction_type="dance", description="desc")
        user = User(name="VK User")
        db.add_all([teacher, direction, user])
        db.flush()

        group = Group(
            direction_id=direction.direction_id,
            teacher_id=teacher.id,
            name="Evening Group",
            age_group="18+",
            max_students=12,
            duration_minutes=60,
            lessons_per_week=2,
        )
        db.add(group)
        db.flush()

        schedule = Schedule(
            object_type="group",
            object_id=group.id,
            group_id=group.id,
            teacher_id=teacher.id,
            title="Evening Group",
            date=date.today() + timedelta(days=1),
            time_from=time(hour=18, minute=0),
            time_to=time(hour=19, minute=0),
            status="scheduled",
        )
        db.add(schedule)
        db.flush()

        db.add(
            NotificationChannel(
                user_id=user.id,
                channel_type="vk",
                target_ref=str(vk_user_id),
                is_enabled=True,
                is_verified=True,
                is_primary=True,
            )
        )
        db.add(
            GroupAbonement(
                user_id=user.id,
                group_id=group.id,
                abonement_type="multi",
                balance_credits=8,
                lessons_total=8,
                status=ABONEMENT_STATUS_ACTIVE,
                valid_from=datetime.combine(schedule.date, time.min),
                valid_to=datetime.combine(schedule.date + timedelta(days=30), time.max),
            )
        )
        db.commit()
        return user.id, schedule.id
    finally:
        db.close()


def test_vk_callback_confirmation_returns_configured_token(app, monkeypatch):
    monkeypatch.setattr(platform_api, "VK_COMMUNITY_ID", "654321")
    monkeypatch.setattr(platform_api, "VK_CALLBACK_CONFIRMATION_TOKEN", "vk-confirm-token")

    response = app.test_client().post(
        "/api/vk/callback",
        json={"type": "confirmation", "group_id": 654321},
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "vk-confirm-token"


def test_vk_callback_message_allow_marks_vk_channel_verified(app, session_factory, monkeypatch):
    db = session_factory()
    try:
        user = User(name="VK Allow User")
        db.add(user)
        db.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type="vk",
            target_ref="700001",
            is_enabled=True,
            is_verified=False,
            is_primary=True,
        )
        db.add(channel)
        db.commit()
        channel_id = channel.id
    finally:
        db.close()

    monkeypatch.setattr(platform_api, "VK_COMMUNITY_ID", "123456")
    monkeypatch.setattr(platform_api, "VK_CALLBACK_SECRET", "callback-secret")

    response = app.test_client().post(
        "/api/vk/callback",
        json={
            "type": "message_allow",
            "group_id": 123456,
            "secret": "callback-secret",
            "object": {
                "user_id": 700001,
            },
        },
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"

    db = session_factory()
    try:
        channel = db.query(NotificationChannel).filter(NotificationChannel.id == channel_id).first()
        assert channel is not None
        assert channel.is_verified is True
        assert channel.is_enabled is True
    finally:
        db.close()


def test_vk_callback_message_deny_marks_vk_channel_unverified(app, session_factory, monkeypatch):
    db = session_factory()
    try:
        user = User(name="VK Deny User")
        db.add(user)
        db.flush()
        channel = NotificationChannel(
            user_id=user.id,
            channel_type="vk",
            target_ref="700002",
            is_enabled=True,
            is_verified=True,
            is_primary=True,
        )
        db.add(channel)
        db.commit()
        channel_id = channel.id
    finally:
        db.close()

    monkeypatch.setattr(platform_api, "VK_COMMUNITY_ID", "123456")
    monkeypatch.setattr(platform_api, "VK_CALLBACK_SECRET", "callback-secret")

    response = app.test_client().post(
        "/api/vk/callback",
        json={
            "type": "message_deny",
            "group_id": 123456,
            "secret": "callback-secret",
            "object": {
                "user_id": 700002,
            },
        },
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"

    db = session_factory()
    try:
        channel = db.query(NotificationChannel).filter(NotificationChannel.id == channel_id).first()
        assert channel is not None
        assert channel.is_verified is False
        assert channel.is_enabled is True
    finally:
        db.close()


def test_vk_callback_message_event_stores_attendance_response_and_answers_with_snackbar(app, session_factory, monkeypatch):
    user_id, schedule_id = _seed_group_schedule_with_vk_channel(session_factory, vk_user_id=778899)
    captured: dict[str, object] = {}
    edited: dict[str, object] = {}

    db = session_factory()
    try:
        db.add(
            AttendanceReminder(
                schedule_id=schedule_id,
                user_id=user_id,
                send_status="sent",
                vk_peer_id=778899,
                vk_message_id=42,
            )
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(platform_api, "VK_COMMUNITY_ID", "123456")
    monkeypatch.setattr(platform_api, "VK_CALLBACK_SECRET", "callback-secret")

    def _fake_answer(*, event_id: str, user_id: int, peer_id: int, event_data: dict | None = None):
        captured["event_id"] = event_id
        captured["user_id"] = user_id
        captured["peer_id"] = peer_id
        captured["event_data"] = event_data
        return {"ok": True}

    def _fake_edit(*, peer_id: int, message_id: int, message: str, payload: dict | None = None):
        edited["peer_id"] = peer_id
        edited["message_id"] = message_id
        edited["message"] = message
        edited["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(platform_api, "send_vk_message_event_answer", _fake_answer)
    monkeypatch.setattr(platform_api, "edit_vk_message", _fake_edit)

    response = app.test_client().post(
        "/api/vk/callback",
        json={
            "type": "message_event",
            "group_id": 123456,
            "secret": "callback-secret",
            "object": {
                "event_id": "evt-001",
                "user_id": 778899,
                "peer_id": 778899,
                "payload": {
                    "command": "attendance_reminder",
                    "action": "will_miss",
                    "schedule_id": schedule_id,
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"
    assert captured == {
        "event_id": "evt-001",
        "user_id": 778899,
        "peer_id": 778899,
        "event_data": {"type": "show_snackbar", "text": "Отметили: не приду"},
    }
    assert edited["peer_id"] == 778899
    assert edited["message_id"] == 42
    assert "Отметили: не приду" in edited["message"]
    assert edited["payload"] == {"keyboard": {"inline": True, "buttons": []}}

    db = session_factory()
    try:
        intention = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user_id).first()
        reminder = db.query(AttendanceReminder).filter_by(schedule_id=schedule_id, user_id=user_id).first()

        assert intention is not None
        assert intention.status == "will_miss"
        assert intention.source == "vk_callback"

        assert reminder is not None
        assert reminder.send_status == "sent"
        assert reminder.response_action == "will_miss"
        assert reminder.responded_at is not None
        assert reminder.button_closed_at is not None
    finally:
        db.close()
