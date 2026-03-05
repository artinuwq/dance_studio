import json
from datetime import datetime

from flask import Blueprint, g, jsonify, request

from dance_studio.core.personal_discounts import DiscountConsumptionConflictError, consume_one_time_discount_for_booking
from dance_studio.db.models import BookingRequest, GroupAbonement, PaymentTransaction
from dance_studio.web.services.access import get_current_user_from_request, require_permission
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


@bp.route("/api/payment-transactions/<int:payment_id>/pay", methods=["POST"])
def pay_transaction(payment_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "User not found"}, 401

    payment = db.query(PaymentTransaction).filter_by(id=payment_id, user_id=user.id).first()
    if not payment:
        return {"error": "Transaction not found"}, 404

    if payment.status == "paid":
        return {"status": "already_paid"}

    booking = None
    abonement = None
    if payment.meta:
        try:
            meta = json.loads(payment.meta)
        except Exception:
            meta = {}
        booking_id = meta.get("booking_id")
        if booking_id:
            booking = db.query(BookingRequest).filter_by(id=booking_id, user_id=user.id).first()
        abonement_id = meta.get("abonement_id")
        if abonement_id:
            abonement = db.query(GroupAbonement).filter_by(id=abonement_id, user_id=user.id).first()

    if booking:
        try:
            consume_one_time_discount_for_booking(db, booking=booking)
        except DiscountConsumptionConflictError:
            db.rollback()
            return {"error": "One-time discount is already consumed for another booking."}, 409

    payment.status = "paid"
    payment.paid_at = datetime.now()

    if not abonement:
        abonement = (
            db.query(GroupAbonement)
            .filter_by(user_id=user.id, status="pending_activation")
            .order_by(GroupAbonement.created_at.desc())
            .first()
        )

    if abonement:
        abonement.status = "active"

    db.commit()
    return {"status": "paid"}


@bp.route("/api/payment-transactions/my", methods=["GET"])
def get_my_transactions():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "User not found"}, 401

    items = db.query(PaymentTransaction).filter_by(user_id=user.id).order_by(PaymentTransaction.created_at.desc()).all()
    result = []
    for t in items:
        result.append(
            {
                "id": t.id,
                "amount": t.amount,
                "currency": t.currency,
                "provider": t.provider,
                "status": t.status,
                "description": t.description,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "paid_at": t.paid_at.isoformat() if t.paid_at else None,
            }
        )

    return jsonify(result)
