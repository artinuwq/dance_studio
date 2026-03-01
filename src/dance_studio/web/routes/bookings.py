import json
from datetime import date, datetime

from flask import Blueprint, current_app, g, jsonify, request

from dance_studio.core.abonement_pricing import (
    ABONEMENT_TYPE_MULTI,
    ABONEMENT_TYPE_TRIAL,
    AbonementPricingError,
    parse_booking_bundle_group_ids,
    quote_group_booking,
    serialize_group_booking_quote,
)
from dance_studio.core.booking_utils import BOOKING_STATUS_LABELS, BOOKING_TYPE_LABELS
from dance_studio.db.models import (
    BookingRequest,
    Direction,
    Group,
    GroupAbonement,
    HallRental,
    IndividualLesson,
    Schedule,
    Staff,
    User,
)
from dance_studio.web.constants import ALLOWED_DIRECTION_TYPES, INACTIVE_SCHEDULE_STATUSES
from dance_studio.web.services.access import _get_current_staff, get_current_user_from_request, require_permission
from dance_studio.web.services.bookings import (
    _compute_duration_minutes,
    _find_booking_overlaps,
    _notify_booking_admins,
    _send_booking_payment_details_via_userbot,
    get_next_group_date,
)
from dance_studio.web.services.payments import _get_active_payment_profile_payload
bp = Blueprint('bookings_routes', __name__)


@bp.route("/api/groups/<int:group_id>", methods=["GET"])
def get_group(group_id):
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ РіСЂСѓРїРїСѓ РїРѕ ID"""
    db = g.db
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Р“СЂСѓРїРїР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404

    teacher_name = group.teacher.name if group.teacher else None
    return jsonify({
        "id": group.id,
        "direction_id": group.direction_id,
        "teacher_id": group.teacher_id,
        "teacher_name": teacher_name,
        "name": group.name,
        "description": group.description,
        "age_group": group.age_group,
        "max_students": group.max_students,
        "duration_minutes": group.duration_minutes,
        "lessons_per_week": group.lessons_per_week,
        "created_at": group.created_at.isoformat()
    })


@bp.route("/api/groups/compatible", methods=["GET"])
def get_compatible_groups():
    db = g.db
    direction_type = (request.args.get("direction_type") or "").strip().lower()
    lessons_per_week_raw = request.args.get("lessons_per_week")
    exclude_group_id_raw = request.args.get("exclude_group_id")

    if direction_type not in ALLOWED_DIRECTION_TYPES:
        return {"error": "direction_type must be dance or sport"}, 400
    try:
        lessons_per_week = int(lessons_per_week_raw)
    except (TypeError, ValueError):
        return {"error": "lessons_per_week must be an integer"}, 400
    if lessons_per_week <= 0:
        return {"error": "lessons_per_week must be > 0"}, 400

    exclude_group_id = None
    if exclude_group_id_raw not in (None, ""):
        try:
            exclude_group_id = int(exclude_group_id_raw)
        except (TypeError, ValueError):
            return {"error": "exclude_group_id must be an integer"}, 400

    groups = db.query(Group).filter(Group.lessons_per_week == lessons_per_week).order_by(Group.created_at.desc()).all()
    direction_ids = {g.direction_id for g in groups if g.direction_id}
    directions = db.query(Direction).filter(Direction.direction_id.in_(direction_ids)).all() if direction_ids else []
    directions_by_id = {d.direction_id: d for d in directions}
    teacher_ids = {g.teacher_id for g in groups if g.teacher_id}
    teachers = db.query(Staff).filter(Staff.id.in_(teacher_ids)).all() if teacher_ids else []
    teachers_by_id = {t.id: t for t in teachers}

    result = []
    for group in groups:
        if exclude_group_id and group.id == exclude_group_id:
            continue
        direction = directions_by_id.get(group.direction_id)
        if not direction:
            continue
        if (direction.direction_type or "").strip().lower() != direction_type:
            continue
        teacher = teachers_by_id.get(group.teacher_id)
        result.append(
            {
                "id": group.id,
                "name": group.name,
                "direction_id": direction.direction_id,
                "direction_title": direction.title,
                "direction_type": direction.direction_type,
                "lessons_per_week": group.lessons_per_week,
                "teacher_name": teacher.name if teacher else None,
            }
        )
    return jsonify(result)


@bp.route("/api/groups/<int:group_id>", methods=["PUT"])
def update_group(group_id):
    """РћР±РЅРѕРІР»СЏРµС‚ РіСЂСѓРїРїСѓ"""
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Р“СЂСѓРїРїР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404

    if "name" in data:
        group.name = data["name"]
    if "description" in data:
        group.description = data["description"]
    if "age_group" in data:
        group.age_group = data["age_group"]
    if "max_students" in data:
        try:
            group.max_students = int(data["max_students"])
        except ValueError:
            return {"error": "max_students РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј"}, 400
    if "duration_minutes" in data:
        try:
            group.duration_minutes = int(data["duration_minutes"])
        except ValueError:
            return {"error": "duration_minutes РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј"}, 400
    if "lessons_per_week" in data:
        if data["lessons_per_week"] in (None, ""):
            group.lessons_per_week = None
        else:
            try:
                group.lessons_per_week = int(data["lessons_per_week"])
            except ValueError:
                return {"error": "lessons_per_week РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј"}, 400
    if "teacher_id" in data:
        teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
        if not teacher:
            return {"error": "РџСЂРµРїРѕРґР°РІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404
        group.teacher_id = data["teacher_id"]

    db.commit()

    return {
        "id": group.id,
        "message": "Р“СЂСѓРїРїР° РѕР±РЅРѕРІР»РµРЅР°"
    }


@bp.route("/api/booking-requests/group/quote", methods=["POST"])
def quote_group_booking_request():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Authentication required"}, 401

    data = request.json or {}
    try:
        quote = quote_group_booking(
            db,
            user_id=user.id,
            group_id=data.get("group_id"),
            abonement_type=data.get("abonement_type"),
            bundle_group_ids=data.get("bundle_group_ids"),
            multi_lessons_per_group=data.get("multi_lessons_per_group"),
        )
    except AbonementPricingError as exc:
        return {"error": str(exc)}, 400

    groups = db.query(Group).filter(Group.id.in_(quote.bundle_group_ids)).all()
    groups_by_id = {row.id: row for row in groups}
    direction_ids = {row.direction_id for row in groups if row.direction_id}
    directions = db.query(Direction).filter(Direction.direction_id.in_(direction_ids)).all() if direction_ids else []
    directions_by_id = {row.direction_id: row for row in directions}

    bundle_groups = []
    for group_id in quote.bundle_group_ids:
        group = groups_by_id.get(group_id)
        direction = directions_by_id.get(group.direction_id) if group else None
        bundle_groups.append(
            {
                "group_id": group_id,
                "group_name": group.name if group else None,
                "direction_id": direction.direction_id if direction else None,
                "direction_title": direction.title if direction else None,
                "direction_type": direction.direction_type if direction else None,
                "lessons_per_week": group.lessons_per_week if group else None,
            }
        )

    payload = serialize_group_booking_quote(quote)
    payload["bundle_groups"] = bundle_groups
    payload["payment_info"] = _get_active_payment_profile_payload(db) if quote.requires_payment else None
    return jsonify(payload)


@bp.route("/api/booking-requests", methods=["GET"])
def list_booking_requests():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    query = db.query(BookingRequest).order_by(BookingRequest.date.asc(), BookingRequest.time_from.asc())
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    if date_from:
        try:
            date_from_val = datetime.strptime(date_from, "%Y-%m-%d").date()
            query = query.filter(BookingRequest.date >= date_from_val)
        except ValueError:
            return {"error": "date_from РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD"}, 400
    if date_to:
        try:
            date_to_val = datetime.strptime(date_to, "%Y-%m-%d").date()
            query = query.filter(BookingRequest.date <= date_to_val)
        except ValueError:
            return {"error": "date_to РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD"}, 400

    result = []
    for booking in query:
        if not booking.date or not booking.time_from or not booking.time_to:
            continue
        if booking.status in {"REJECTED", "CANCELLED"}:
            continue
        time_from_str = booking.time_from.strftime("%H:%M") if booking.time_from else None
        time_to_str = booking.time_to.strftime("%H:%M") if booking.time_to else None
        result.append({
            "id": booking.id,
            "object_type": booking.object_type,
            "group_id": booking.group_id,
            "abonement_type": booking.abonement_type,
            "bundle_group_ids": parse_booking_bundle_group_ids(booking),
            "teacher_id": booking.teacher_id,
            "date": booking.date.isoformat(),
            "time_from": time_from_str,
            "time_to": time_to_str,
            "status": booking.status,
            "status_label": BOOKING_STATUS_LABELS.get(booking.status, booking.status),
            "user_name": booking.user_name,
            "comment": booking.comment,
            "lessons_count": booking.lessons_count,
            "requested_amount": booking.requested_amount,
            "requested_currency": booking.requested_currency,
            "group_start_date": booking.group_start_date.isoformat() if booking.group_start_date else None,
            "valid_until": booking.valid_until.isoformat() if booking.valid_until else None,
        })

    return jsonify(result)


@bp.route("/api/booking-requests/my", methods=["GET"])
def list_my_booking_requests():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Authentication required"}, 401

    rows = (
        db.query(BookingRequest)
        .filter(BookingRequest.user_id == user.id)
        .order_by(BookingRequest.created_at.desc(), BookingRequest.id.desc())
        .all()
    )

    all_group_ids: set[int] = set()
    for booking in rows:
        bundle_ids = parse_booking_bundle_group_ids(booking)
        for group_id in bundle_ids:
            all_group_ids.add(int(group_id))
        if booking.group_id:
            all_group_ids.add(int(booking.group_id))

    groups_by_id: dict[int, Group] = {}
    if all_group_ids:
        groups = db.query(Group).filter(Group.id.in_(list(all_group_ids))).all()
        groups_by_id = {int(group.id): group for group in groups}

    result = []
    for booking in rows:
        bundle_group_ids = parse_booking_bundle_group_ids(booking)
        if booking.group_id and int(booking.group_id) not in bundle_group_ids:
            bundle_group_ids.insert(0, int(booking.group_id))

        bundle_group_names = []
        for group_id in bundle_group_ids:
            group = groups_by_id.get(int(group_id))
            bundle_group_names.append(group.name if group and group.name else f"Р“СЂСѓРїРїР° #{group_id}")

        main_group = groups_by_id.get(int(booking.group_id)) if booking.group_id else None
        result.append(
            {
                "id": booking.id,
                "object_type": booking.object_type,
                "object_type_label": BOOKING_TYPE_LABELS.get(booking.object_type, booking.object_type),
                "status": booking.status,
                "status_label": BOOKING_STATUS_LABELS.get(booking.status, booking.status),
                "comment": booking.comment,
                "created_at": booking.created_at.isoformat() if booking.created_at else None,
                "date": booking.date.isoformat() if booking.date else None,
                "time_from": booking.time_from.strftime("%H:%M") if booking.time_from else None,
                "time_to": booking.time_to.strftime("%H:%M") if booking.time_to else None,
                "teacher_id": booking.teacher_id,
                "group_id": booking.group_id,
                "group_name": main_group.name if main_group else None,
                "bundle_group_ids": bundle_group_ids,
                "bundle_group_names": bundle_group_names,
                "abonement_type": booking.abonement_type,
                "lessons_count": booking.lessons_count,
                "requested_amount": booking.requested_amount,
                "requested_currency": booking.requested_currency,
                "group_start_date": booking.group_start_date.isoformat() if booking.group_start_date else None,
                "valid_until": booking.valid_until.isoformat() if booking.valid_until else None,
            }
        )

    return jsonify(result)


@bp.route("/api/booking-requests", methods=["POST"])
def create_booking_request():
    db = g.db
    data = request.json or {}

    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Authentication required"}, 401

    object_type = data.get("object_type")
    if object_type not in ["rental", "individual", "group"]:
        return {"error": "object_type must be rental, individual, or group"}, 400

    teacher_id_val = None
    if "teacher_id" in data:
        try:
            teacher_id_val = int(data.get("teacher_id"))
        except (TypeError, ValueError):
            return {"error": "teacher_id must be an integer"}, 400
        teacher = db.query(Staff).filter_by(id=teacher_id_val, status="active").first()
        if not teacher:
            return {"error": "Teacher not found"}, 404

    if object_type == "individual" and not teacher_id_val:
        return {"error": "teacher_id is required for individual booking"}, 400

    date_str = data.get("date")
    time_from_str = data.get("time_from")
    time_to_str = data.get("time_to")
    comment = data.get("comment")

    date_val = None
    time_from_val = None
    time_to_val = None
    group_id_val = None
    lessons_count_val = None
    group_start_date_val = None
    valid_until_val = None
    requested_amount_val = None
    requested_currency_val = None
    abonement_type_val = None
    bundle_group_ids_json_val = None
    quote_payload = None
    overlaps: list[dict] = []

    if object_type != "group":
        if not date_str or not time_from_str or not time_to_str:
            return {"error": "date, time_from and time_to are required"}, 400
        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
            time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
            time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
        except ValueError:
            return {"error": "Invalid date/time format. Expected YYYY-MM-DD and HH:MM"}, 400

        if object_type == "rental" and date_val < date.today():
            return {"error": "Rental date cannot be in the past"}, 400
        if time_from_val >= time_to_val:
            return {"error": "time_from must be earlier than time_to"}, 400

        overlaps = _find_booking_overlaps(db, date_val, time_from_val, time_to_val)
        status = "NEW"
    else:
        try:
            quote = quote_group_booking(
                db,
                user_id=user.id,
                group_id=data.get("group_id"),
                abonement_type=data.get("abonement_type"),
                bundle_group_ids=data.get("bundle_group_ids"),
                multi_lessons_per_group=data.get("multi_lessons_per_group"),
            )
        except AbonementPricingError as exc:
            return {"error": str(exc)}, 400

        quote_payload = serialize_group_booking_quote(quote)
        group_id_val = quote.group_id
        lessons_count_val = quote.total_lessons
        group_start_date_val = quote.valid_from.date()
        valid_until_val = quote.valid_to.date()
        requested_amount_val = quote.amount
        requested_currency_val = quote.currency
        abonement_type_val = quote.abonement_type
        bundle_group_ids_json_val = json.dumps(quote.bundle_group_ids, ensure_ascii=False)
        status = "NEW" if (quote.abonement_type == ABONEMENT_TYPE_TRIAL and quote.amount == 0) else "AWAITING_PAYMENT"

    booking = BookingRequest(
        user_id=user.id,
        user_telegram_id=user.telegram_id,
        user_name=user.name,
        user_username=user.username,
        object_type=object_type,
        date=date_val,
        time_from=time_from_val,
        time_to=time_to_val,
        duration_minutes=_compute_duration_minutes(time_from_val, time_to_val),
        comment=comment,
        overlaps_json=json.dumps(overlaps, ensure_ascii=False),
        status=status,
        teacher_id=teacher_id_val,
        group_id=group_id_val,
        abonement_type=abonement_type_val,
        bundle_group_ids_json=bundle_group_ids_json_val,
        lessons_count=lessons_count_val,
        requested_amount=requested_amount_val,
        requested_currency=requested_currency_val,
        group_start_date=group_start_date_val,
        valid_until=valid_until_val,
    )
    db.add(booking)
    db.flush()

    if object_type == "rental" and date_val and time_from_val and time_to_val:
        rental = HallRental(
            creator_id=user.id,
            creator_type="user",
            date=date_val,
            time_from=time_from_val,
            time_to=time_to_val,
            purpose=comment,
            review_status="pending",
            payment_status="pending",
            activity_status="pending",
            comment=comment,
            start_time=datetime.combine(date_val, time_from_val),
            end_time=datetime.combine(date_val, time_to_val),
            status=status,
            duration_minutes=booking.duration_minutes,
        )
        db.add(rental)
        db.flush()

        rental_schedule = Schedule(
            object_type="rental",
            object_id=rental.id,
            date=date_val,
            time_from=time_from_val,
            time_to=time_to_val,
            status=status,
            status_comment=f"Synced with booking #{booking.id}",
            title="РђСЂРµРЅРґР° Р·Р°Р»Р°",
            start_time=time_from_val,
            end_time=time_to_val,
        )
        db.add(rental_schedule)

    if object_type == "individual" and teacher_id_val and date_val and time_from_val and time_to_val:
        individual_lesson = IndividualLesson(
            teacher_id=teacher_id_val,
            student_id=user.id,
            date=date_val,
            time_from=time_from_val,
            time_to=time_to_val,
            duration_minutes=booking.duration_minutes,
            comment=comment,
            person_comment=comment,
            booking_id=booking.id,
            status=status,
        )
        db.add(individual_lesson)
        db.flush()

        lesson_schedule = Schedule(
            object_type="individual",
            object_id=individual_lesson.id,
            date=date_val,
            time_from=time_from_val,
            time_to=time_to_val,
            status=status,
            title="РРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРµ Р·Р°РЅСЏС‚РёРµ",
            start_time=time_from_val,
            end_time=time_to_val,
            teacher_id=teacher_id_val,
        )
        db.add(lesson_schedule)

    db.commit()

    _notify_booking_admins(booking, user)
    if object_type == "group" and int(booking.requested_amount or 0) > 0:
        _send_booking_payment_details_via_userbot(db, booking, user)

    response_payload = {
        "id": booking.id,
        "status": booking.status,
        "overlaps": overlaps,
    }
    if object_type == "group":
        response_payload.update(
            {
                "group_id": booking.group_id,
                "abonement_type": booking.abonement_type,
                "bundle_group_ids": parse_booking_bundle_group_ids(booking),
                "lessons_count": booking.lessons_count,
                "requested_amount": booking.requested_amount,
                "requested_currency": booking.requested_currency,
                "group_start_date": booking.group_start_date.isoformat() if booking.group_start_date else None,
                "valid_until": booking.valid_until.isoformat() if booking.valid_until else None,
                "quote": quote_payload,
                "payment_info": _get_active_payment_profile_payload(db) if int(booking.requested_amount or 0) > 0 else None,
            }
        )

    return response_payload, 201


@bp.route("/api/booking-requests/teacher-individual", methods=["POST"])
def create_teacher_individual_booking_request():
    """
    Создаёт заявку на индивидуальное занятие от имени тренера за выбранного клиента.
    Этап брони НЕ пропускается: заявка создаётся со статусом NEW и ждёт подтверждения админов.
    """
    perm_error = require_permission("view_personal_lessons")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}

    staff = _get_current_staff(db)
    if not staff:
        return {"error": "Staff profile not found"}, 403

    student_user_id_raw = data.get("student_user_id")
    try:
        student_user_id = int(student_user_id_raw)
    except (TypeError, ValueError):
        return {"error": "student_user_id must be an integer"}, 400

    student = db.query(User).filter_by(id=student_user_id).first()
    if not student:
        return {"error": "Student not found"}, 404

    date_str = data.get("date")
    time_from_str = data.get("time_from")
    time_to_str = data.get("time_to")
    if not date_str or not time_from_str or not time_to_str:
        return {"error": "date, time_from and time_to are required"}, 400

    try:
        date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
        time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
    except ValueError:
        return {"error": "Invalid date/time format. Expected YYYY-MM-DD and HH:MM"}, 400

    if time_from_val >= time_to_val:
        return {"error": "time_from must be earlier than time_to"}, 400

    comment_raw = (data.get("comment") or "").strip()
    teacher_note = f"Создано тренером: {staff.name} (staff_id={staff.id})"
    comment = f"{comment_raw}\n\n{teacher_note}" if comment_raw else teacher_note

    overlaps = _find_booking_overlaps(db, date_val, time_from_val, time_to_val)
    status = "NEW"
    duration_minutes = _compute_duration_minutes(time_from_val, time_to_val)

    booking = BookingRequest(
        user_id=student.id,
        user_telegram_id=student.telegram_id,
        user_name=student.name,
        user_username=student.username,
        object_type="individual",
        date=date_val,
        time_from=time_from_val,
        time_to=time_to_val,
        duration_minutes=duration_minutes,
        comment=comment,
        overlaps_json=json.dumps(overlaps, ensure_ascii=False),
        status=status,
        teacher_id=staff.id,
    )
    db.add(booking)
    db.flush()

    individual_lesson = IndividualLesson(
        teacher_id=staff.id,
        student_id=student.id,
        date=date_val,
        time_from=time_from_val,
        time_to=time_to_val,
        duration_minutes=duration_minutes,
        comment=comment,
        person_comment=comment,
        booking_id=booking.id,
        status=status,
    )
    db.add(individual_lesson)
    db.flush()

    lesson_schedule = Schedule(
        object_type="individual",
        object_id=individual_lesson.id,
        date=date_val,
        time_from=time_from_val,
        time_to=time_to_val,
        status=status,
        status_comment=f"Synced with booking #{booking.id}",
        title="Индивидуальное занятие",
        start_time=time_from_val,
        end_time=time_to_val,
        teacher_id=staff.id,
    )
    db.add(lesson_schedule)
    db.commit()

    _notify_booking_admins(booking, student)

    return {
        "id": booking.id,
        "status": booking.status,
        "overlaps": overlaps,
        "lesson_id": individual_lesson.id,
        "schedule_id": lesson_schedule.id,
    }, 201


@bp.route("/api/rental-occupancy")
def rental_occupancy():
    db = g.db
    date_str = request.args.get("date")
    if not date_str:
        date_val = datetime.now().date()
    else:
        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD Рё Р±С‹С‚СЊ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµР№ РґР°С‚РѕР№"}, 400

    entries = db.query(Schedule).filter(
        Schedule.object_type == "rental",
        Schedule.date == date_val,
        Schedule.time_from.isnot(None),
        Schedule.time_to.isnot(None),
        Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES))
    ).all()

    result = []
    for entry in entries:
        result.append({
            "id": entry.id,
            "date": entry.date.isoformat() if entry.date else None,
            "time_from": entry.time_from.strftime("%H:%M") if entry.time_from else None,
            "time_to": entry.time_to.strftime("%H:%M") if entry.time_to else None,
            "status": entry.status,
            "title": entry.title or "РђСЂРµРЅРґР°"
        })

    return jsonify(result), 200


@bp.route("/api/hall-occupancy")
def hall_occupancy():
    db = g.db
    date_str = request.args.get("date")
    if not date_str:
        date_val = datetime.now().date()
    else:
        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD Рё Р±С‹С‚СЊ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµР№ РґР°С‚РѕР№"}, 400

    entries = db.query(Schedule).filter(
        Schedule.date == date_val,
        Schedule.time_from.isnot(None),
        Schedule.time_to.isnot(None),
        Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES))
    ).order_by(Schedule.time_from.asc()).all()

    result = []
    for entry in entries:
        result.append({
            "id": entry.id,
            "date": entry.date.isoformat() if entry.date else None,
            "time_from": entry.time_from.strftime("%H:%M") if entry.time_from else None,
            "time_to": entry.time_to.strftime("%H:%M") if entry.time_to else None,
            "status": entry.status,
            "title": entry.title or "РЎРѕР±С‹С‚РёРµ",
            "object_type": entry.object_type
        })

    current_app.logger.info("hall occupancy %s -> %s entries", date_val, len(result))
    return jsonify(result), 200


@bp.route("/api/individual-lessons/<int:lesson_id>")
def get_individual_lesson(lesson_id):
    db = g.db
    lesson = db.query(IndividualLesson).filter_by(id=lesson_id).first()
    if not lesson:
        return {"error": "РЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРµ Р·Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    teacher = db.query(Staff).filter_by(id=lesson.teacher_id).first()
    student = db.query(User).filter_by(id=lesson.student_id).first()

    return jsonify({
        "id": lesson.id,
        "date": lesson.date.isoformat() if lesson.date else None,
        "time_from": lesson.time_from.strftime("%H:%M") if lesson.time_from else None,
        "time_to": lesson.time_to.strftime("%H:%M") if lesson.time_to else None,
        "status": lesson.status,
        "teacher": {
            "id": teacher.id if teacher else None,
            "name": teacher.name if teacher else "вЂ”"
        },
        "student": {
            "id": student.id if student else None,
            "name": student.name if student else "вЂ”",
            "telegram_id": student.telegram_id if student else None,
            "username": student.username if student else None
        }
    })


@bp.route("/api/groups/<int:group_id>/next-session", methods=["GET"])
def get_group_next_session(group_id: int):
    db = g.db
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Group not found"}, 404
    next_date = get_next_group_date(db, group_id)
    return jsonify({"group_id": group_id, "next_session_date": next_date.isoformat() if next_date else None})


@bp.route("/api/group-abonements/create", methods=["POST"])
def create_group_abonement():
    db = g.db
    data = request.json or {}

    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Authentication required"}, 401

    raw_group_id = data.get("group_id")
    try:
        group_id = int(raw_group_id)
    except (TypeError, ValueError):
        return {"error": "group_id must be an integer"}, 400

    raw_bundle_group_ids = data.get("bundle_group_ids")
    if raw_bundle_group_ids in (None, "", []):
        raw_bundle_group_ids = [group_id]

    try:
        quote = quote_group_booking(
            db,
            user_id=user.id,
            group_id=group_id,
            abonement_type=data.get("abonement_type") or ABONEMENT_TYPE_MULTI,
            bundle_group_ids=raw_bundle_group_ids,
            multi_lessons_per_group=data.get("multi_lessons_per_group"),
        )
    except AbonementPricingError as exc:
        return {"error": str(exc)}, 400

    legacy_lessons_count = data.get("lessons_count")
    if legacy_lessons_count not in (None, ""):
        try:
            legacy_lessons_count = int(legacy_lessons_count)
        except (TypeError, ValueError):
            return {"error": "lessons_count must be an integer"}, 400
        if legacy_lessons_count != quote.total_lessons:
            return {
                "error": f"lessons_count mismatch: expected {quote.total_lessons} for selected abonement configuration"
            }, 400

    status = "NEW" if (quote.abonement_type == ABONEMENT_TYPE_TRIAL and quote.amount == 0) else "AWAITING_PAYMENT"
    booking = BookingRequest(
        user_id=user.id,
        user_telegram_id=user.telegram_id,
        user_name=user.name,
        user_username=user.username,
        object_type="group",
        status=status,
        comment=(data.get("comment") or "").strip() or None,
        group_id=quote.group_id,
        abonement_type=quote.abonement_type,
        bundle_group_ids_json=json.dumps(quote.bundle_group_ids, ensure_ascii=False),
        lessons_count=quote.total_lessons,
        requested_amount=quote.amount,
        requested_currency=quote.currency,
        group_start_date=quote.valid_from.date(),
        valid_until=quote.valid_to.date(),
        overlaps_json=json.dumps([], ensure_ascii=False),
    )
    db.add(booking)
    db.commit()

    _notify_booking_admins(booking, user)
    if quote.requires_payment:
        _send_booking_payment_details_via_userbot(db, booking, user)

    return (
        jsonify(
            {
                "ok": True,
                "booking_id": booking.id,
                "status": booking.status,
                "abonement_type": booking.abonement_type,
                "bundle_group_ids": parse_booking_bundle_group_ids(booking),
                "amount": booking.requested_amount,
                "currency": booking.requested_currency or "RUB",
                "valid_from": quote.valid_from.isoformat(),
                "valid_to": quote.valid_to.isoformat(),
                "payment_id": None,
                "payment_info": _get_active_payment_profile_payload(db) if quote.requires_payment else None,
            }
        ),
        201,
    )


@bp.route("/api/group-abonements/my", methods=["GET"])
def get_my_abonements():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 401

    items = db.query(GroupAbonement).filter_by(user_id=user.id, status="active").order_by(GroupAbonement.created_at.desc()).all()
    result = []
    for a in items:
        group = db.query(Group).filter_by(id=a.group_id).first()
        direction = db.query(Direction).filter_by(direction_id=group.direction_id).first() if group else None
        result.append({
            "id": a.id,
            "group_id": a.group_id,
            "group_name": group.name if group else None,
            "direction_title": direction.title if direction else None,
            "abonement_type": a.abonement_type,
            "bundle_id": a.bundle_id,
            "bundle_size": a.bundle_size,
            "balance_credits": a.balance_credits,
            "status": a.status,
            "valid_from": a.valid_from.isoformat() if a.valid_from else None,
            "valid_to": a.valid_to.isoformat() if a.valid_to else None
        })

    return jsonify(result)


@bp.route("/api/groups/my", methods=["GET"])
def get_my_groups():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 401

    abonements = db.query(GroupAbonement).filter_by(user_id=user.id, status="active").all()
    group_ids = sorted({a.group_id for a in abonements})
    result = []
    for group_id in group_ids:
        group = db.query(Group).filter_by(id=group_id).first()
        if not group:
            continue
        direction = db.query(Direction).filter_by(direction_id=group.direction_id).first()
        teacher = db.query(Staff).filter_by(id=group.teacher_id).first()
        result.append({
            "group_id": group.id,
            "group_name": group.name,
            "direction_title": direction.title if direction else None,
            "teacher_name": teacher.name if teacher else None
        })

    return jsonify(result)



