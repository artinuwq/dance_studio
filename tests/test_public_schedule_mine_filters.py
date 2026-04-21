from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.core.statuses import ABONEMENT_STATUS_ACTIVE
from dance_studio.core.time import utcnow
from dance_studio.db.models import Base, Direction, Group, GroupAbonement, Schedule, SessionRecord, Staff, User
from dance_studio.web.app import create_app
from dance_studio.web.services.auth_session import _sid_hash


@pytest.fixture
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


def _create_session(db, *, telegram_id: int, user_id: int) -> str:
    sid = secrets.token_hex(16)
    now = utcnow()
    db.add(
        SessionRecord(
            id=secrets.token_hex(32),
            telegram_id=telegram_id,
            user_id=user_id,
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


def test_schedule_public_mine_keeps_client_items_but_excludes_staff_teaching_groups(app, session_factory):
    db = session_factory()
    try:
        linked_telegram_id = 502001
        user = User(name="Trainer User", telegram_id=linked_telegram_id)
        linked_staff = Staff(
            name="Linked Trainer",
            telegram_id=linked_telegram_id,
            user=user,
            position="teacher",
            status="active",
        )
        other_staff = Staff(
            name="Other Teacher",
            telegram_id=linked_telegram_id + 1,
            position="teacher",
            status="active",
        )
        direction = Direction(title="Dance Mix", direction_type="dance", status="active")
        db.add_all([user, linked_staff, other_staff, direction])
        db.flush()

        taught_group = Group(
            direction_id=direction.direction_id,
            teacher_id=linked_staff.id,
            name="Леди",
            age_group="18+",
            max_students=16,
            duration_minutes=60,
            lessons_per_week=2,
        )
        subscribed_group = Group(
            direction_id=direction.direction_id,
            teacher_id=other_staff.id,
            name="Комбат 1",
            age_group="18+",
            max_students=16,
            duration_minutes=60,
            lessons_per_week=2,
        )
        db.add_all([taught_group, subscribed_group])
        db.flush()

        today = date.today()
        db.add_all(
            [
                Schedule(
                    object_type="group",
                    object_id=taught_group.id,
                    group_id=taught_group.id,
                    teacher_id=linked_staff.id,
                    title=taught_group.name,
                    date=today + timedelta(days=1),
                    time_from=time(19, 0),
                    time_to=time(20, 0),
                    status="scheduled",
                ),
                Schedule(
                    object_type="group",
                    object_id=subscribed_group.id,
                    group_id=subscribed_group.id,
                    teacher_id=other_staff.id,
                    title=subscribed_group.name,
                    date=today + timedelta(days=2),
                    time_from=time(18, 0),
                    time_to=time(19, 0),
                    status="scheduled",
                ),
                GroupAbonement(
                    user_id=user.id,
                    group_id=subscribed_group.id,
                    abonement_type="multi",
                    balance_credits=8,
                    status=ABONEMENT_STATUS_ACTIVE,
                    valid_from=datetime.combine(today - timedelta(days=1), time.min),
                    valid_to=datetime.combine(today + timedelta(days=30), time.max),
                ),
            ]
        )
        db.commit()

        sid = _create_session(db, telegram_id=linked_telegram_id, user_id=user.id)
    finally:
        db.close()

    client = app.test_client()
    client.set_cookie("sid", sid)

    response = client.get("/schedule/public?mine=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert [item["title"] for item in payload] == ["Комбат 1"]
