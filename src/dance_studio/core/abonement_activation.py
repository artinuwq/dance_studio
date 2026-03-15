from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
import uuid

from dance_studio.core.abonement_pricing import (
    ABONEMENT_TYPE_MULTI,
    ABONEMENT_TYPE_SINGLE,
    ABONEMENT_TYPE_TRIAL,
    is_free_trial_booking,
    parse_booking_bundle_group_ids,
)
from dance_studio.core.system_settings_service import get_setting_value
from dance_studio.core.statuses import (
    ABONEMENT_STATUS_ACTIVE,
    ABONEMENT_STATUS_PENDING_PAYMENT,
    set_abonement_status,
)
from dance_studio.db.models import (
    BookingRequest,
    Direction,
    Group,
    GroupAbonement,
    PaymentTransaction,
)


def _compute_booking_payment_amount(db, booking: BookingRequest) -> int | None:
    for raw_amount in (booking.requested_amount, booking.amount_before_discount):
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError):
            continue
        if amount >= 0:
            return amount

    if booking.object_type != "group":
        return None
    if not booking.group_id or not booking.lessons_count:
        return None

    try:
        lessons_count = int(booking.lessons_count)
    except (TypeError, ValueError):
        return None
    if lessons_count <= 0:
        return None

    group = db.query(Group).filter_by(id=booking.group_id).first()
    if not group:
        return None
    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first()
    if not direction or not direction.base_price:
        return None
    try:
        base_price = int(direction.base_price)
    except (TypeError, ValueError):
        return None
    if base_price <= 0:
        return None

    return lessons_count * base_price


def _coerce_non_negative_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _resolve_booking_total_amount(db, booking: BookingRequest, abonement_type: str) -> int | None:
    for raw_amount in (booking.requested_amount, booking.amount_before_discount):
        parsed = _coerce_non_negative_int(raw_amount)
        if parsed is not None:
            return parsed

    if abonement_type == ABONEMENT_TYPE_TRIAL:
        try:
            return int(get_setting_value(db, "abonements.trial_price_rub"))
        except Exception:
            return None

    return None


def activate_group_abonement_from_booking(db, booking: BookingRequest) -> GroupAbonement | None:
    if booking.object_type != "group":
        return None
    if not booking.user_id or not booking.group_id:
        return None

    amount = _compute_booking_payment_amount(db, booking)
    requires_confirmed_payment = bool((amount or 0) > 0 and not is_free_trial_booking(booking))
    if requires_confirmed_payment:
        # Session uses autoflush=False; flush to make in-transaction confirmed
        # payments visible before activation checks.
        db.flush()
        confirmed_payment = (
            db.query(PaymentTransaction.id)
            .filter_by(payment_type="booking", object_id=booking.id, status="confirmed")
            .first()
        )
        if not confirmed_payment:
            return None

    bundle_group_ids = parse_booking_bundle_group_ids(booking)
    if not bundle_group_ids:
        bundle_group_ids = [int(booking.group_id)]
    if int(booking.group_id) not in bundle_group_ids:
        bundle_group_ids.insert(0, int(booking.group_id))
    bundle_size = max(1, min(3, len(bundle_group_ids)))
    bundle_group_ids = bundle_group_ids[:bundle_size]

    abonement_type = (booking.abonement_type or ABONEMENT_TYPE_MULTI).strip().lower()
    if abonement_type not in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_MULTI, ABONEMENT_TYPE_TRIAL}:
        abonement_type = ABONEMENT_TYPE_MULTI

    total_lessons = 0
    try:
        total_lessons = int(booking.lessons_count or 0)
    except (TypeError, ValueError):
        total_lessons = 0

    if abonement_type in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_TRIAL}:
        lessons_per_group = 1
        total_lessons = 1
    elif total_lessons > 0 and total_lessons % bundle_size == 0:
        lessons_per_group = max(1, total_lessons // bundle_size)
    else:
        base_group = db.query(Group).filter_by(id=int(booking.group_id)).first()
        fallback_per_week = 1
        if base_group and base_group.lessons_per_week not in (None, ""):
            try:
                fallback_per_week = max(1, int(base_group.lessons_per_week))
            except (TypeError, ValueError):
                fallback_per_week = 1
        lessons_per_group = fallback_per_week * 4
        if total_lessons <= 0:
            total_lessons = lessons_per_group * bundle_size

    total_amount = _resolve_booking_total_amount(db, booking, abonement_type)
    price_per_lesson = None
    if total_amount is not None and total_lessons > 0:
        price_per_lesson = max(0, int(total_amount) // int(total_lessons))

    if booking.group_start_date:
        valid_from = datetime.combine(booking.group_start_date, dt_time.min)
    else:
        valid_from = datetime.utcnow()

    if booking.valid_until:
        valid_to = datetime.combine(booking.valid_until, dt_time.max)
    elif abonement_type in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_TRIAL}:
        valid_to = datetime.combine(valid_from.date(), dt_time.max)
    else:
        valid_to = valid_from + timedelta(days=30)

    existing_rows = (
        db.query(GroupAbonement)
        .filter(
            GroupAbonement.user_id == booking.user_id,
            GroupAbonement.status == ABONEMENT_STATUS_ACTIVE,
            GroupAbonement.group_id.in_(bundle_group_ids),
            GroupAbonement.abonement_type == abonement_type,
            GroupAbonement.bundle_size == bundle_size,
            GroupAbonement.valid_from == valid_from,
            GroupAbonement.valid_to == valid_to,
            GroupAbonement.balance_credits == lessons_per_group,
        )
        .all()
    )
    if len(existing_rows) == bundle_size:
        by_group_id = {row.group_id: row for row in existing_rows}
        if all(group_id in by_group_id for group_id in bundle_group_ids):
            return by_group_id[bundle_group_ids[0]]

    bundle_id = str(uuid.uuid4()) if bundle_size > 1 else None
    activated: list[GroupAbonement] = []
    for group_id in bundle_group_ids:
        pending_row = (
            db.query(GroupAbonement)
            .filter_by(user_id=booking.user_id, group_id=group_id, status=ABONEMENT_STATUS_PENDING_PAYMENT)
            .order_by(GroupAbonement.created_at.desc())
            .first()
        )
        if pending_row:
            set_abonement_status(pending_row, ABONEMENT_STATUS_ACTIVE)
            pending_row.balance_credits = lessons_per_group
            pending_row.valid_from = valid_from
            pending_row.valid_to = valid_to
            pending_row.abonement_type = abonement_type
            pending_row.bundle_size = bundle_size
            pending_row.bundle_id = bundle_id
            pending_row.price_total_rub = total_amount
            pending_row.lessons_total = total_lessons if total_lessons > 0 else None
            pending_row.price_per_lesson_rub = price_per_lesson
            activated.append(pending_row)
            continue

        abonement = GroupAbonement(
            user_id=booking.user_id,
            group_id=group_id,
            abonement_type=abonement_type,
            bundle_id=bundle_id,
            bundle_size=bundle_size,
            balance_credits=lessons_per_group,
            price_total_rub=total_amount,
            lessons_total=total_lessons if total_lessons > 0 else None,
            price_per_lesson_rub=price_per_lesson,
            status=ABONEMENT_STATUS_ACTIVE,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        db.add(abonement)
        activated.append(abonement)

    return activated[0] if activated else None


__all__ = ["activate_group_abonement_from_booking"]
