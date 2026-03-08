from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request
from sqlalchemy import func

from dance_studio.core.statuses import (
    ABONEMENT_STATUS_ACTIVE,
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_CONFIRMED,
    BOOKING_STATUS_WAITING_PAYMENT,
    normalize_booking_status,
    set_abonement_status,
    set_booking_status,
)
from dance_studio.db.models import BookingRequest, GroupAbonement, PaymentTransaction, Staff, User
from dance_studio.web.services.access import _get_current_staff, get_current_user_from_request, require_permission
from dance_studio.web.services.bookings import (
    BookingReservationExpiredError,
    expire_stale_booking_reservations,
    is_booking_reservation_expired,
)
from dance_studio.web.services.payments import (
    PAYMENT_PROFILE_DEFAULT_TITLES,
    PAYMENT_PROFILE_SECONDARY_SLOTS,
    PAYMENT_PROFILE_SLOTS,
    _ensure_payment_profiles,
    _get_active_payment_profile_payload,
    _get_secondary_owner_active_slot,
    _serialize_payment_profile,
)

bp = Blueprint("payments_routes", __name__)
ALLOWED_PAYMENT_TYPES = {"booking", "abonement"}
ALLOWED_PAYMENT_STATUSES = {"confirmed", "rejected"}


def _parse_positive_int(raw_value, field_name: str) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} должен быть целым числом")
    if parsed <= 0:
        raise ValueError(f"{field_name} должен быть > 0")
    return parsed


def _parse_optional_date(value, field_name: str):
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} должен быть в формате YYYY-MM-DD") from exc


def _resolve_payment_target(db, payment_type: str, object_id: int):
    if payment_type == "booking":
        booking = db.query(BookingRequest).filter_by(id=object_id).with_for_update().first()
        if not booking:
            raise ValueError("Бронь не найдена")
        return booking, booking.user_id

    abonement = db.query(GroupAbonement).filter_by(id=object_id).with_for_update().first()
    if not abonement:
        raise ValueError("Абонемент не найден")
    return abonement, abonement.user_id


def _serialize_payment_transaction(db, payment: PaymentTransaction) -> dict:
    user = db.query(User).filter_by(id=payment.user_id).first() if payment.user_id else None
    admin = db.query(Staff).filter_by(id=payment.confirmed_by_admin).first() if payment.confirmed_by_admin else None
    return {
        "id": payment.id,
        "user_id": payment.user_id,
        "user_name": user.name if user else None,
        "amount": payment.amount,
        "status": payment.status,
        "payment_type": payment.payment_type,
        "object_id": payment.object_id,
        "confirmed_by_admin": payment.confirmed_by_admin,
        "confirmed_by_admin_name": admin.name if admin else None,
        "confirmed_at": payment.confirmed_at.isoformat() if payment.confirmed_at else None,
        "created_at": payment.created_at.isoformat() if payment.created_at else None,
        "comment": payment.comment,
    }


def _booking_default_amount(booking: BookingRequest) -> int | None:
    for raw in (booking.requested_amount, booking.amount_before_discount):
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _apply_payment_effects(
    target,
    *,
    payment_type: str,
    status: str,
    confirmed_at: datetime,
    actor_staff: Staff | None,
) -> None:
    if status != "confirmed":
        return

    if payment_type == "booking":
        set_booking_status(
            target,
            BOOKING_STATUS_CONFIRMED,
            actor_staff_id=(actor_staff.id if actor_staff else None),
            actor_name=(actor_staff.name if actor_staff else None),
            changed_at=confirmed_at,
        )
        target.reserved_until = None
        return

    set_abonement_status(target, ABONEMENT_STATUS_ACTIVE)


def _create_manual_payment(
    db,
    *,
    payment_type: str,
    object_id: int,
    amount: int,
    status: str,
    comment: str | None,
) -> PaymentTransaction:
    if payment_type not in ALLOWED_PAYMENT_TYPES:
        raise ValueError("payment_type должен быть booking или abonement")
    if status not in ALLOWED_PAYMENT_STATUSES:
        raise ValueError("status должен быть confirmed или rejected")

    target, user_id = _resolve_payment_target(db, payment_type, object_id)
    if not user_id:
        raise ValueError("У целевого объекта не найден user_id")

    now = datetime.utcnow()
    if payment_type == "booking" and status == "confirmed":
        expired_booking_ids = expire_stale_booking_reservations(
            db,
            now=now,
            booking_id=int(target.id),
        )
        if int(target.id) in expired_booking_ids:
            raise BookingReservationExpiredError("Резерв истек")
        if is_booking_reservation_expired(target, now=now):
            raise BookingReservationExpiredError("Резерв истек")

        normalized_status = normalize_booking_status(target.status)
        if normalized_status == BOOKING_STATUS_CANCELLED:
            raise ValueError("Бронь отменена")
        if normalized_status == BOOKING_STATUS_WAITING_PAYMENT and target.reserved_until is None:
            raise BookingReservationExpiredError("Резерв истек")

    duplicate_confirmed = (
        db.query(PaymentTransaction.id)
        .filter_by(payment_type=payment_type, object_id=object_id, status="confirmed")
        .first()
    )
    if duplicate_confirmed and status == "confirmed":
        raise ValueError("Оплата по этому объекту уже подтверждена")

    actor_staff = _get_current_staff(db)

    payment = PaymentTransaction(
        user_id=int(user_id),
        amount=amount,
        status=status,
        payment_type=payment_type,
        object_id=object_id,
        confirmed_by_admin=((actor_staff.id if actor_staff else None) if status == "confirmed" else None),
        confirmed_at=(now if status == "confirmed" else None),
        comment=comment,
    )
    db.add(payment)

    _apply_payment_effects(
        target,
        payment_type=payment_type,
        status=status,
        confirmed_at=now,
        actor_staff=actor_staff,
    )

    db.commit()
    return payment


@bp.route("/api/payment-profiles/active", methods=["GET"])
def get_active_payment_profile():
    db = g.db
    profile = _get_active_payment_profile_payload(db)
    if not profile:
        return {"error": "Реквизиты оплаты не настроены"}, 404
    return jsonify(profile)


@bp.route("/api/admin/payment-profiles", methods=["GET"])
def admin_get_payment_profiles():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    profiles = _ensure_payment_profiles(db)
    db.commit()
    result = [_serialize_payment_profile(profiles[slot]) for slot in PAYMENT_PROFILE_SLOTS]
    active_slot = _get_secondary_owner_active_slot(db)
    return jsonify({"profiles": result, "active_slot": active_slot})


@bp.route("/api/admin/payment-profiles/<int:slot>", methods=["PUT"])
def admin_update_payment_profile(slot):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    if slot not in PAYMENT_PROFILE_SLOTS:
        return {"error": "slot должен быть одним из: 1, 2, 3"}, 400

    db = g.db
    data = request.json or {}
    recipient_bank = str(data.get("recipient_bank") or "").strip()
    recipient_number = str(data.get("recipient_number") or "").strip()
    recipient_full_name = str(data.get("recipient_full_name") or "").strip()

    if not recipient_bank:
        return {"error": "Поле recipient_bank обязательно"}, 400
    if not recipient_number:
        return {"error": "Поле recipient_number обязательно"}, 400
    if not recipient_full_name:
        return {"error": "Поле recipient_full_name обязательно"}, 400
    if len(recipient_bank) > 160:
        return {"error": "Поле recipient_bank слишком длинное (макс. 160)"}, 400
    if len(recipient_number) > 64:
        return {"error": "Поле recipient_number слишком длинное (макс. 64)"}, 400
    if len(recipient_full_name) > 160:
        return {"error": "Поле recipient_full_name слишком длинное (макс. 160)"}, 400

    profiles = _ensure_payment_profiles(db)
    profile = profiles[slot]
    profile.title = PAYMENT_PROFILE_DEFAULT_TITLES.get(slot) or f"Реквизиты {slot}"
    profile.details = (
        f"Банк получателя: {recipient_bank}\n"
        f"Номер: {recipient_number}\n"
        f"ФИО получателя: {recipient_full_name}"
    )
    profile.recipient_bank = recipient_bank
    profile.recipient_number = recipient_number
    profile.recipient_full_name = recipient_full_name
    db.commit()
    return jsonify({"profile": _serialize_payment_profile(profile)})


@bp.route("/api/admin/payment-profiles/active", methods=["PUT"])
def admin_switch_active_payment_profile():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    data = request.json or {}
    try:
        active_slot = int(data.get("active_slot"))
    except (TypeError, ValueError):
        return {"error": "active_slot должен быть 2 или 3"}, 400

    if active_slot not in PAYMENT_PROFILE_SECONDARY_SLOTS:
        return {"error": "active_slot должен быть 2 или 3"}, 400

    db = g.db
    profiles = _ensure_payment_profiles(db)
    for slot in PAYMENT_PROFILE_SECONDARY_SLOTS:
        profile = profiles.get(slot)
        if profile:
            profile.is_active = slot == active_slot
    slot_one = profiles.get(1)
    if slot_one:
        slot_one.is_active = False
    db.commit()
    return jsonify({"active_slot": active_slot})


@bp.route("/api/payment-transactions/my", methods=["GET"])
def get_my_transactions():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "User not found"}, 401

    items = db.query(PaymentTransaction).filter_by(user_id=user.id).order_by(PaymentTransaction.created_at.desc()).all()
    result = []
    for item in items:
        result.append(_serialize_payment_transaction(db, item))

    return jsonify(result)


@bp.route("/api/admin/payments", methods=["GET"])
def admin_list_payments():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    query = db.query(PaymentTransaction)

    user_id_raw = request.args.get("user_id")
    payment_type_raw = str(request.args.get("payment_type") or "").strip().lower()
    status_raw = str(request.args.get("status") or "").strip().lower()
    date_from_raw = request.args.get("date_from")
    date_to_raw = request.args.get("date_to")

    if user_id_raw not in (None, ""):
        try:
            user_id = _parse_positive_int(user_id_raw, "user_id")
        except ValueError as exc:
            return {"error": str(exc)}, 400
        query = query.filter(PaymentTransaction.user_id == user_id)

    if payment_type_raw:
        if payment_type_raw not in ALLOWED_PAYMENT_TYPES:
            return {"error": "payment_type должен быть booking или abonement"}, 400
        query = query.filter(PaymentTransaction.payment_type == payment_type_raw)

    if status_raw:
        if status_raw not in ALLOWED_PAYMENT_STATUSES:
            return {"error": "status должен быть confirmed или rejected"}, 400
        query = query.filter(PaymentTransaction.status == status_raw)

    try:
        date_from = _parse_optional_date(date_from_raw, "date_from")
        date_to = _parse_optional_date(date_to_raw, "date_to")
    except ValueError as exc:
        return {"error": str(exc)}, 400

    if date_from and date_to and date_to < date_from:
        return {"error": "date_to не может быть раньше date_from"}, 400

    effective_ts = func.coalesce(PaymentTransaction.confirmed_at, PaymentTransaction.created_at)
    if date_from:
        dt_from = datetime.combine(date_from, datetime.min.time())
        query = query.filter(effective_ts >= dt_from)
    if date_to:
        dt_to = datetime.combine(date_to, datetime.min.time()) + timedelta(days=1)
        query = query.filter(effective_ts < dt_to)

    rows = query.order_by(PaymentTransaction.confirmed_at.desc(), PaymentTransaction.created_at.desc()).all()
    return jsonify({"items": [_serialize_payment_transaction(db, row) for row in rows]})


@bp.route("/api/admin/booking-requests/<int:booking_id>/confirm-payment", methods=["POST"])
def admin_confirm_booking_payment(booking_id: int):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    booking = db.query(BookingRequest).filter_by(id=booking_id).first()
    if not booking:
        return {"error": "Бронь не найдена"}, 404

    payload = request.json or {}
    status = str(payload.get("status") or "confirmed").strip().lower()
    comment_raw = payload.get("comment")
    comment = str(comment_raw).strip() if comment_raw is not None else None

    raw_amount = payload.get("amount")
    if raw_amount in (None, ""):
        amount = _booking_default_amount(booking)
        if not amount:
            return {"error": "Не удалось определить сумму оплаты, передайте amount вручную"}, 400
    else:
        try:
            amount = _parse_positive_int(raw_amount, "amount")
        except ValueError as exc:
            return {"error": str(exc)}, 400

    try:
        payment = _create_manual_payment(
            db,
            payment_type="booking",
            object_id=booking.id,
            amount=amount,
            status=status,
            comment=comment,
        )
    except BookingReservationExpiredError as exc:
        db.rollback()
        return {"error": str(exc)}, 409
    except ValueError as exc:
        db.rollback()
        return {"error": str(exc)}, 400

    return jsonify(
        {
            "ok": True,
            "payment": _serialize_payment_transaction(db, payment),
            "booking_status": booking.status,
        }
    )


@bp.route("/api/admin/group-abonements/<int:abonement_id>/confirm-payment", methods=["POST"])
def admin_confirm_abonement_payment(abonement_id: int):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "Абонемент не найден"}, 404

    payload = request.json or {}
    status = str(payload.get("status") or "confirmed").strip().lower()
    comment_raw = payload.get("comment")
    comment = str(comment_raw).strip() if comment_raw is not None else None

    try:
        amount = _parse_positive_int(payload.get("amount"), "amount")
    except ValueError as exc:
        return {"error": str(exc)}, 400

    try:
        payment = _create_manual_payment(
            db,
            payment_type="abonement",
            object_id=abonement.id,
            amount=amount,
            status=status,
            comment=comment,
        )
    except ValueError as exc:
        db.rollback()
        return {"error": str(exc)}, 400

    return jsonify(
        {
            "ok": True,
            "payment": _serialize_payment_transaction(db, payment),
            "abonement_status": abonement.status,
        }
    )
