from datetime import date, datetime, timedelta, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dance_studio.core.statuses import (
    ABONEMENT_STATUS_ACTIVE,
    ABONEMENT_STATUS_CANCELLED,
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_CREATED,
    BOOKING_STATUS_WAITING_PAYMENT,
)
from dance_studio.db.models import Base, BookingRequest, Direction, Group, GroupAbonement, Staff, User
from dance_studio.web.services.bookings import (
    BookingAlreadyExistsError,
    BookingCapacityExceededError,
    create_booking_request_with_guards,
    expire_stale_booking_reservations,
)


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_group(db, *, max_students: int = 2) -> Group:
    teacher = Staff(name="Teacher", position="teacher", status="active")
    direction = Direction(title="Hip-Hop", direction_type="dance", status="active")
    db.add_all([teacher, direction])
    db.flush()

    group = Group(
        direction_id=direction.direction_id,
        teacher_id=teacher.id,
        name="Kids A",
        age_group="10-12",
        max_students=max_students,
        duration_minutes=60,
    )
    db.add(group)
    db.flush()
    return group


def _seed_user(db, *, name: str) -> User:
    user = User(name=name)
    db.add(user)
    db.flush()
    return user


def test_non_group_booking_duplicate_is_blocked():
    db = _make_session()
    try:
        user = _seed_user(db, name="User One")
        now = datetime(2026, 3, 8, 10, 0, 0)

        booking_1 = BookingRequest(
            user_id=user.id,
            object_type="individual",
            date=date(2026, 3, 10),
            time_from=time(18, 0),
            time_to=time(19, 0),
            status=BOOKING_STATUS_CREATED,
        )
        create_booking_request_with_guards(db, booking_1, now=now)

        booking_2 = BookingRequest(
            user_id=user.id,
            object_type="individual",
            date=date(2026, 3, 10),
            time_from=time(18, 0),
            time_to=time(19, 0),
            status=BOOKING_STATUS_CREATED,
        )
        with pytest.raises(BookingAlreadyExistsError):
            create_booking_request_with_guards(db, booking_2, now=now)
    finally:
        db.close()


def test_group_capacity_is_blocked_when_full():
    db = _make_session()
    try:
        group = _seed_group(db, max_students=1)
        user_1 = _seed_user(db, name="User One")
        user_2 = _seed_user(db, name="User Two")
        now = datetime(2026, 3, 8, 12, 0, 0)

        booking_1 = BookingRequest(
            user_id=user_1.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            status=BOOKING_STATUS_WAITING_PAYMENT,
        )
        create_booking_request_with_guards(db, booking_1, now=now)

        booking_2 = BookingRequest(
            user_id=user_2.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            status=BOOKING_STATUS_WAITING_PAYMENT,
        )
        with pytest.raises(BookingCapacityExceededError):
            create_booking_request_with_guards(db, booking_2, now=now)
    finally:
        db.close()


def test_group_pending_duplicate_is_replaced_by_new_booking():
    db = _make_session()
    try:
        group = _seed_group(db, max_students=3)
        user = _seed_user(db, name="User One")
        now = datetime(2026, 3, 8, 14, 0, 0)

        booking_1 = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            status=BOOKING_STATUS_WAITING_PAYMENT,
        )
        create_booking_request_with_guards(db, booking_1, now=now)

        booking_2 = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            status=BOOKING_STATUS_CREATED,
        )
        create_booking_request_with_guards(db, booking_2, now=now)

        assert booking_1.status == BOOKING_STATUS_CANCELLED
        assert booking_1.reserved_until is None
        assert booking_2.id is not None
    finally:
        db.close()


def test_group_confirmed_duplicate_with_active_abonement_is_blocked():
    db = _make_session()
    try:
        group = _seed_group(db, max_students=3)
        user = _seed_user(db, name="User One")
        now = datetime(2026, 3, 8, 14, 0, 0)

        booking_1 = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            valid_until=date(2026, 4, 9),
            status=BOOKING_STATUS_CONFIRMED,
        )
        db.add(booking_1)
        db.flush()
        db.add(
            GroupAbonement(
                user_id=user.id,
                group_id=group.id,
                balance_credits=4,
                status=ABONEMENT_STATUS_ACTIVE,
                valid_from=datetime(2026, 3, 10, 0, 0, 0),
                valid_to=datetime(2026, 4, 9, 23, 59, 59),
            )
        )
        db.flush()

        booking_2 = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            valid_until=date(2026, 4, 9),
            status=BOOKING_STATUS_CREATED,
        )
        with pytest.raises(BookingAlreadyExistsError):
            create_booking_request_with_guards(db, booking_2, now=now)
    finally:
        db.close()


def test_group_duplicate_is_ignored_when_old_abonement_was_cancelled():
    db = _make_session()
    try:
        group = _seed_group(db, max_students=3)
        user = _seed_user(db, name="User One")
        now = datetime(2026, 3, 8, 14, 0, 0)

        old_booking = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            valid_until=date(2026, 4, 9),
            status=BOOKING_STATUS_CONFIRMED,
        )
        db.add(old_booking)
        db.flush()

        db.add(
            GroupAbonement(
                user_id=user.id,
                group_id=group.id,
                balance_credits=0,
                status=ABONEMENT_STATUS_CANCELLED,
                valid_from=datetime(2026, 3, 10, 0, 0, 0),
                valid_to=datetime(2026, 4, 9, 23, 59, 59),
            )
        )
        db.flush()

        new_booking = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            valid_until=date(2026, 4, 9),
            status=BOOKING_STATUS_WAITING_PAYMENT,
        )
        create_booking_request_with_guards(db, new_booking, now=now)

        assert old_booking.status == BOOKING_STATUS_CANCELLED
        assert new_booking.id is not None
        assert new_booking.reserved_until is not None
    finally:
        db.close()


def test_group_capacity_ignores_old_booking_with_cancelled_abonement():
    db = _make_session()
    try:
        group = _seed_group(db, max_students=1)
        former_user = _seed_user(db, name="Former Student")
        new_user = _seed_user(db, name="New Student")
        now = datetime(2026, 3, 8, 15, 0, 0)

        old_booking = BookingRequest(
            user_id=former_user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            valid_until=date(2026, 4, 9),
            status=BOOKING_STATUS_CONFIRMED,
        )
        db.add(old_booking)
        db.flush()

        db.add(
            GroupAbonement(
                user_id=former_user.id,
                group_id=group.id,
                balance_credits=0,
                status=ABONEMENT_STATUS_CANCELLED,
                valid_from=datetime(2026, 3, 10, 0, 0, 0),
                valid_to=datetime(2026, 4, 9, 23, 59, 59),
            )
        )
        db.flush()

        new_booking = BookingRequest(
            user_id=new_user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 10),
            valid_until=date(2026, 4, 9),
            status=BOOKING_STATUS_WAITING_PAYMENT,
        )
        create_booking_request_with_guards(db, new_booking, now=now)

        assert old_booking.status == BOOKING_STATUS_CANCELLED
        assert new_booking.id is not None
        assert new_booking.reserved_until is not None
    finally:
        db.close()


def test_waiting_payment_reservation_expires_to_cancelled():
    db = _make_session()
    try:
        group = _seed_group(db, max_students=2)
        user = _seed_user(db, name="User One")
        now = datetime(2026, 3, 8, 16, 0, 0)

        booking = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group.id,
            group_start_date=date(2026, 3, 11),
            status=BOOKING_STATUS_WAITING_PAYMENT,
        )
        create_booking_request_with_guards(db, booking, now=now)
        assert booking.reserved_until is not None

        expired_ids = expire_stale_booking_reservations(
            db,
            now=now + timedelta(hours=48, minutes=1),
            booking_id=booking.id,
        )
        assert booking.id in expired_ids
        assert booking.status == BOOKING_STATUS_CANCELLED
        assert booking.reserved_until is None
    finally:
        db.close()
