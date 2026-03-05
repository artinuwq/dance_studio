from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dance_studio.core.personal_discounts import (
    DiscountConsumptionConflictError,
    apply_best_discount,
    consume_one_time_discount_for_booking,
)
from dance_studio.db.models import Base, BookingRequest, User, UserDiscount


def _discount(*, discount_id, discount_type, value, is_active=True, is_one_time=True, created_at=None):
    return SimpleNamespace(
        id=discount_id,
        discount_type=discount_type,
        value=value,
        is_active=is_active,
        is_one_time=is_one_time,
        created_at=created_at or datetime(2026, 3, 5, 12, 0, 0),
    )


def test_apply_best_discount_picks_best_and_clamps():
    result = apply_best_discount(
        5000,
        [
            _discount(discount_id=1, discount_type="percentage", value=10),  # 500
            _discount(discount_id=2, discount_type="fixed", value=1000),  # 1000
        ],
    )
    assert result.amount_before_discount == 5000
    assert result.discount_amount == 1000
    assert result.final_amount == 4000
    assert result.discount_id == 2

    clamp_result = apply_best_discount(500, [_discount(discount_id=3, discount_type="fixed", value=3000)])
    assert clamp_result.discount_amount == 500
    assert clamp_result.final_amount == 0


def test_apply_best_discount_tie_break_prefers_reusable_then_newer():
    same_time = datetime(2026, 3, 5, 13, 0, 0)
    result = apply_best_discount(
        1000,
        [
            _discount(discount_id=10, discount_type="fixed", value=200, is_one_time=True, created_at=same_time),
            _discount(discount_id=11, discount_type="fixed", value=200, is_one_time=False, created_at=same_time),
        ],
    )
    assert result.discount_id == 11

    newer = apply_best_discount(
        1000,
        [
            _discount(discount_id=20, discount_type="fixed", value=200, is_one_time=False, created_at=datetime(2026, 3, 5, 10)),
            _discount(discount_id=21, discount_type="fixed", value=200, is_one_time=False, created_at=datetime(2026, 3, 5, 11)),
        ],
    )
    assert newer.discount_id == 21


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_consume_one_time_discount_marks_consumed():
    db = _make_session()
    try:
        user = User(name="User A")
        db.add(user)
        db.flush()

        discount = UserDiscount(
            user_id=user.id,
            discount_type="fixed",
            value=500,
            is_one_time=True,
            is_active=True,
        )
        db.add(discount)
        db.flush()

        booking = BookingRequest(
            user_id=user.id,
            object_type="group",
            status="AWAITING_PAYMENT",
            applied_discount_id=discount.id,
            requested_amount=1000,
            amount_before_discount=1500,
            applied_discount_amount=500,
        )
        db.add(booking)
        db.commit()

        changed = consume_one_time_discount_for_booking(db, booking=booking)
        db.commit()

        reloaded = db.query(UserDiscount).filter_by(id=discount.id).first()
        assert changed is True
        assert reloaded is not None
        assert reloaded.is_active is False
        assert reloaded.consumed_booking_id == booking.id
        assert reloaded.consumed_at is not None
    finally:
        db.close()


def test_consume_one_time_discount_conflicts_on_second_booking():
    db = _make_session()
    try:
        user = User(name="User B")
        db.add(user)
        db.flush()

        discount = UserDiscount(
            user_id=user.id,
            discount_type="percentage",
            value=20,
            is_one_time=True,
            is_active=True,
        )
        db.add(discount)
        db.flush()

        booking_1 = BookingRequest(
            user_id=user.id,
            object_type="group",
            status="AWAITING_PAYMENT",
            applied_discount_id=discount.id,
            requested_amount=800,
            amount_before_discount=1000,
            applied_discount_amount=200,
        )
        booking_2 = BookingRequest(
            user_id=user.id,
            object_type="group",
            status="AWAITING_PAYMENT",
            applied_discount_id=discount.id,
            requested_amount=800,
            amount_before_discount=1000,
            applied_discount_amount=200,
        )
        db.add_all([booking_1, booking_2])
        db.commit()

        first_changed = consume_one_time_discount_for_booking(db, booking=booking_1)
        db.commit()
        assert first_changed is True

        with pytest.raises(DiscountConsumptionConflictError):
            consume_one_time_discount_for_booking(db, booking=booking_2)
    finally:
        db.close()
