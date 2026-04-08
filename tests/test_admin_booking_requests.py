import json
import os
import secrets
from datetime import date, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.core.permissions import ROLES
from dance_studio.core.time import utcnow
from dance_studio.db.models import (
    Base,
    BookingRequest,
    Direction,
    Group,
    IndividualLesson,
    Schedule,
    SessionRecord,
    Staff,
    User,
)
from dance_studio.web.app import create_app
from dance_studio.web.routes import bookings as bookings_routes
from dance_studio.web.services.auth_session import _sid_hash


def _pick_role_with(permission: str) -> str:
    for role, spec in ROLES.items():
        if permission in spec.get("permissions", []):
            return role
    return next(iter(ROLES))


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
    monkeypatch.setattr(bookings_routes, "enqueue_booking_payment_details_delivery", lambda *args, **kwargs: None)
    return create_app()


def _login_by_session(client, db, user_id: int, telegram_id: int | None = None):
    sid = secrets.token_hex(16)
    now = utcnow()
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
    client.set_cookie("sid", sid)


def _seed_admin(db, *, telegram_id: int = 720001) -> User:
    admin_user = User(name="Admin", telegram_id=telegram_id)
    db.add(admin_user)
    db.flush()
    db.add(
        Staff(
            name="Admin Staff",
            telegram_id=telegram_id,
            user_id=admin_user.id,
            position=_pick_role_with("manage_schedule"),
            status="active",
        )
    )
    db.commit()
    return admin_user


def _seed_group_bundle(db):
    teacher = Staff(name="Teacher", position="teacher", status="active")
    direction = Direction(title="Contemporary", direction_type="dance", base_price=2500, status="active")
    db.add_all([teacher, direction])
    db.flush()
    group = Group(
        direction_id=direction.direction_id,
        teacher_id=teacher.id,
        name="Contemporary 18+",
        age_group="18+",
        max_students=12,
        duration_minutes=60,
        lessons_per_week=2,
    )
    db.add(group)
    db.commit()
    return teacher, group


def test_admin_booking_requests_list_includes_pending_and_archive_items(app, session_factory):
    db = session_factory()
    admin_user = _seed_admin(db)
    client_user = User(
        name="Client One",
        telegram_id=720002,
        username="client_one",
        phone="+79990000001",
        primary_phone="+79990000001",
        email="client@example.com",
    )
    db.add(client_user)
    db.commit()
    teacher, group = _seed_group_bundle(db)

    group_booking = BookingRequest(
        user_id=client_user.id,
        user_name=client_user.name,
        user_username=client_user.username,
        object_type="group",
        group_id=group.id,
        abonement_type="multi",
        bundle_group_ids_json=json.dumps([group.id]),
        lessons_count=8,
        requested_amount=6400,
        requested_currency="RUB",
        group_start_date=date(2026, 4, 10),
        valid_until=date(2026, 5, 10),
        status="created",
    )
    cancelled_booking = BookingRequest(
        user_id=client_user.id,
        user_name=client_user.name,
        user_username=client_user.username,
        object_type="individual",
        teacher_id=teacher.id,
        date=date(2026, 4, 11),
        time_from=time(18, 0),
        time_to=time(19, 0),
        duration_minutes=60,
        requested_amount=2500,
        requested_currency="RUB",
        status="cancelled",
    )
    db.add_all([group_booking, cancelled_booking])
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, admin_user.telegram_id)

    response = client.get("/api/admin/booking-requests")
    assert response.status_code == 200

    payload = response.get_json()
    assert isinstance(payload, dict)
    items = {item["id"]: item for item in payload["items"]}

    pending_item = items[group_booking.id]
    assert pending_item["group_name"] == "Contemporary 18+"
    assert pending_item["teacher_name"] == "Teacher"
    assert pending_item["user_phone"] == "+79990000001"
    assert pending_item["user_email"] == "client@example.com"
    assert pending_item["can_approve"] is True
    assert pending_item["can_cancel"] is True
    assert pending_item["can_confirm_payment"] is False

    archived_item = items[cancelled_booking.id]
    assert archived_item["status"] == "cancelled"
    assert archived_item["teacher_name"] == "Teacher"
    assert archived_item["can_approve"] is False
    assert archived_item["can_cancel"] is False


def test_admin_can_approve_then_cancel_paid_individual_booking(app, session_factory):
    db = session_factory()
    admin_user = _seed_admin(db, telegram_id=720101)
    client_user = User(name="Client Two", telegram_id=720102)
    db.add(client_user)
    db.commit()
    teacher, _group = _seed_group_bundle(db)

    booking = BookingRequest(
        user_id=client_user.id,
        user_name=client_user.name,
        object_type="individual",
        teacher_id=teacher.id,
        date=date(2026, 4, 12),
        time_from=time(17, 0),
        time_to=time(18, 0),
        duration_minutes=60,
        requested_amount=2500,
        requested_currency="RUB",
        status="created",
    )
    db.add(booking)
    db.flush()

    lesson = IndividualLesson(
        teacher_id=teacher.id,
        student_id=client_user.id,
        date=booking.date,
        time_from=booking.time_from,
        time_to=booking.time_to,
        duration_minutes=60,
        booking_id=booking.id,
        status="created",
    )
    db.add(lesson)
    db.flush()

    schedule = Schedule(
        object_type="individual",
        object_id=lesson.id,
        date=booking.date,
        time_from=booking.time_from,
        time_to=booking.time_to,
        status="created",
        title="Individual",
        start_time=booking.time_from,
        end_time=booking.time_to,
        teacher_id=teacher.id,
    )
    db.add(schedule)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, admin_user.telegram_id)

    approve_response = client.post(f"/api/admin/booking-requests/{booking.id}/approve")
    assert approve_response.status_code == 200

    db.expire_all()
    refreshed_booking = db.query(BookingRequest).filter_by(id=booking.id).first()
    refreshed_lesson = db.query(IndividualLesson).filter_by(id=lesson.id).first()
    refreshed_schedule = db.query(Schedule).filter_by(id=schedule.id).first()

    assert refreshed_booking is not None
    assert refreshed_booking.status == "waiting_payment"
    assert refreshed_booking.reserved_until is not None
    assert refreshed_lesson is not None and refreshed_lesson.status == "waiting_payment"
    assert refreshed_schedule is not None and refreshed_schedule.status == "waiting_payment"

    cancel_response = client.post(f"/api/admin/booking-requests/{booking.id}/cancel")
    assert cancel_response.status_code == 200

    db.expire_all()
    refreshed_booking = db.query(BookingRequest).filter_by(id=booking.id).first()
    refreshed_lesson = db.query(IndividualLesson).filter_by(id=lesson.id).first()
    refreshed_schedule = db.query(Schedule).filter_by(id=schedule.id).first()

    assert refreshed_booking is not None
    assert refreshed_booking.status == "cancelled"
    assert refreshed_booking.reserved_until is None
    assert refreshed_lesson is not None and refreshed_lesson.status == "cancelled"
    assert refreshed_schedule is not None and refreshed_schedule.status == "cancelled"


def test_admin_approve_free_booking_confirms_immediately(app, session_factory):
    db = session_factory()
    admin_user = _seed_admin(db, telegram_id=720201)
    client_user = User(name="Client Three", telegram_id=720202)
    db.add(client_user)
    db.commit()
    _teacher, group = _seed_group_bundle(db)

    booking = BookingRequest(
        user_id=client_user.id,
        user_name=client_user.name,
        object_type="group",
        group_id=group.id,
        abonement_type="trial",
        bundle_group_ids_json=json.dumps([group.id]),
        lessons_count=1,
        requested_amount=0,
        requested_currency="RUB",
        group_start_date=date(2026, 4, 15),
        valid_until=date(2026, 4, 15),
        status="created",
    )
    db.add(booking)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, admin_user.telegram_id)

    response = client.post(f"/api/admin/booking-requests/{booking.id}/approve")
    assert response.status_code == 200

    db.expire_all()
    refreshed_booking = db.query(BookingRequest).filter_by(id=booking.id).first()
    assert refreshed_booking is not None
    assert refreshed_booking.status == "confirmed"
    assert refreshed_booking.reserved_until is None
