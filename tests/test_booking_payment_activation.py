import json
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dance_studio.core.statuses import ABONEMENT_STATUS_ACTIVE, BOOKING_STATUS_CONFIRMED, BOOKING_STATUS_WAITING_PAYMENT
from dance_studio.db.models import Base, BookingRequest, Direction, Group, GroupAbonement, Staff, User
from dance_studio.web.routes import payments as payments_routes


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_group_bundle(db) -> tuple[Group, Group]:
    teacher = Staff(name="Teacher", position="teacher", status="active")
    direction = Direction(title="Lady Dance", direction_type="dance", base_price=1000, status="active")
    db.add_all([teacher, direction])
    db.flush()

    group_1 = Group(
        direction_id=direction.direction_id,
        teacher_id=teacher.id,
        name="LADY DANCE #1",
        age_group="16+",
        max_students=20,
        duration_minutes=60,
        lessons_per_week=2,
    )
    group_2 = Group(
        direction_id=direction.direction_id,
        teacher_id=teacher.id,
        name="LADY DANCE #2",
        age_group="16+",
        max_students=20,
        duration_minutes=60,
        lessons_per_week=2,
    )
    db.add_all([group_1, group_2])
    db.flush()
    return group_1, group_2


def test_confirmed_booking_payment_activates_bundle_abonements(monkeypatch):
    db = _make_session()
    try:
        monkeypatch.setattr(payments_routes, "_get_current_staff", lambda _db: None)

        user = User(name="Mariana")
        db.add(user)
        db.flush()
        group_1, group_2 = _seed_group_bundle(db)

        booking = BookingRequest(
            user_id=user.id,
            object_type="group",
            group_id=group_1.id,
            abonement_type="multi",
            bundle_group_ids_json=json.dumps([group_1.id, group_2.id], ensure_ascii=False),
            lessons_count=8,
            requested_amount=6400,
            requested_currency="RUB",
            group_start_date=date(2026, 3, 14),
            valid_until=date(2026, 4, 11),
            status=BOOKING_STATUS_WAITING_PAYMENT,
            reserved_until=datetime.utcnow() + timedelta(days=1),
        )
        db.add(booking)
        db.commit()

        payments_routes._create_manual_payment(
            db,
            payment_type="booking",
            object_id=booking.id,
            amount=6400,
            status="confirmed",
            comment="Manual payment confirmation",
        )

        refreshed_booking = db.query(BookingRequest).filter_by(id=booking.id).first()
        assert refreshed_booking is not None
        assert refreshed_booking.status == BOOKING_STATUS_CONFIRMED
        assert refreshed_booking.reserved_until is None

        abonements = (
            db.query(GroupAbonement)
            .filter_by(user_id=user.id, status=ABONEMENT_STATUS_ACTIVE)
            .order_by(GroupAbonement.group_id.asc())
            .all()
        )
        assert len(abonements) == 2
        assert {row.group_id for row in abonements} == {group_1.id, group_2.id}
        assert all(row.bundle_size == 2 for row in abonements)
        assert all(row.balance_credits == 4 for row in abonements)
        assert all(row.valid_from is not None and row.valid_from.date() == date(2026, 3, 14) for row in abonements)
        assert all(row.valid_to is not None and row.valid_to.date() == date(2026, 4, 11) for row in abonements)
        assert len({row.bundle_id for row in abonements}) == 1
        assert all(row.bundle_id for row in abonements)
    finally:
        db.close()
