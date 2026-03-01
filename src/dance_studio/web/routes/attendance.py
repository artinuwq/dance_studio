from datetime import datetime

from flask import Blueprint, g, request

from dance_studio.core.permissions import has_permission
from dance_studio.db.models import Attendance, AttendanceIntention, IndividualLesson, Schedule, User
from dance_studio.web.constants import (
    ATTENDANCE_ALLOWED_STATUSES,
    ATTENDANCE_INTENTION_LOCKED_MESSAGE,
    ATTENDANCE_INTENTION_STATUS_WILL_MISS,
)
from dance_studio.web.services.access import _get_current_staff, get_current_user_from_request
from dance_studio.web.services.attendance import (
    _attendance_already_debited,
    _attendance_intention_lock_info,
    _attendance_marking_window_info,
    _can_edit_schedule_attendance,
    _can_user_set_absence_for_schedule,
    _debit_abonement_for_attendance,
    _load_group_roster,
    _serialize_attendance_intention_with_lock,
)
bp = Blueprint('attendance_routes', __name__)


@bp.route("/api/attendance/<int:schedule_id>", methods=["GET"])
def get_attendance(schedule_id):
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    if not _can_edit_schedule_attendance(db, schedule):
        return {"error": "РќРµС‚ РґРѕСЃС‚СѓРїР°"}, 403

    existing = {a.user_id: a for a in db.query(Attendance).filter_by(schedule_id=schedule_id).all()}
    intentions = {
        row.user_id: row
        for row in db.query(AttendanceIntention).filter_by(schedule_id=schedule_id).all()
    }
    window = _attendance_marking_window_info(schedule)
    items = []
    roster_source = None

    if schedule.object_type == "group":
        roster_source = "group"
        for row in _load_group_roster(db, schedule):
            user = row["user"]
            abon = row.get("abonement")
            att = existing.pop(user.id, None)
            planned = intentions.pop(user.id, None)
            planned_status = "will_miss" if (planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS) else "will_come"
            items.append({
                "user_id": user.id,
                "name": user.name,
                "username": user.username,
                "phone": user.phone,
                "status": att.status if att else None,
                "comment": att.comment if att else None,
                "abonement_id": att.abonement_id if att else (abon.id if abon else None),
                "debited": _attendance_already_debited(db, att.id) if att else False,
                "planned_absence": bool(planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS),
                "planned_absence_reason": planned.reason if planned else None,
                "planned_status": planned_status,
            })
    elif schedule.object_type == "individual":
        roster_source = "individual"
        lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first() if schedule.object_id else None
        if lesson and lesson.student_id:
            user = db.query(User).filter_by(id=lesson.student_id).first()
            if user:
                att = existing.pop(user.id, None)
                planned = intentions.pop(user.id, None)
                planned_status = "will_miss" if (planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS) else "will_come"
                items.append({
                    "user_id": user.id,
                    "name": user.name,
                    "username": user.username,
                    "phone": user.phone,
                    "status": att.status if att else None,
                    "comment": att.comment if att else None,
                    "abonement_id": att.abonement_id if att else None,
                    "debited": _attendance_already_debited(db, att.id) if att else False,
                    "planned_absence": bool(planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS),
                    "planned_absence_reason": planned.reason if planned else None,
                    "planned_status": planned_status,
                })

    # add remaining manual/legacy attendance
    for att in existing.values():
        user = db.query(User).filter_by(id=att.user_id).first()
        planned = intentions.pop(att.user_id, None)
        planned_status = "will_miss" if (planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS) else "will_come"
        items.append({
            "user_id": att.user_id,
            "name": user.name if user else None,
            "username": user.username if user else None,
            "phone": user.phone if user else None,
            "status": att.status,
            "comment": att.comment,
            "abonement_id": att.abonement_id,
            "debited": _attendance_already_debited(db, att.id),
            "planned_absence": bool(planned and planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS),
            "planned_absence_reason": planned.reason if planned else None,
            "planned_status": planned_status,
        })

    for planned in intentions.values():
        user = db.query(User).filter_by(id=planned.user_id).first()
        items.append({
            "user_id": planned.user_id,
            "name": user.name if user else None,
            "username": user.username if user else None,
            "phone": user.phone if user else None,
            "status": None,
            "comment": None,
            "abonement_id": None,
            "debited": False,
            "planned_absence": planned.status == ATTENDANCE_INTENTION_STATUS_WILL_MISS,
            "planned_absence_reason": planned.reason,
            "planned_status": "will_miss",
        })

    status_labels = {
        "present": "РџСЂРёСЃСѓС‚СЃС‚РІРѕРІР°Р»",
        "absent": "РћС‚СЃСѓС‚СЃС‚РІРѕРІР°Р»",
        "late": "РћРїРѕР·РґР°Р»",
        "sick": "Р‘РѕР»РµР»",
    }

    return {
        "items": items,
        "source": roster_source or "manual",
        "status_labels": status_labels,
        "debit_policy": "РЎРїРёСЃС‹РІР°РµС‚СЃСЏ 1 Р·Р°РЅСЏС‚РёРµ РґР»СЏ РІСЃРµС… СЃС‚Р°С‚СѓСЃРѕРІ, РєСЂРѕРјРµ 'sick'",
        "can_edit": bool(window["is_open"]),
        "attendance_phase": window["phase"],
        "attendance_phase_message": window["message"],
        "attendance_starts_at": window["starts_at"],
        "attendance_mark_until": window["ends_at"],
        "planned_summary": {
            "will_come": sum(1 for i in items if i.get("planned_status") == "will_come"),
            "will_miss": sum(1 for i in items if i.get("planned_status") == "will_miss"),
        },
    }


@bp.route("/api/attendance/<int:schedule_id>", methods=["POST"])
def set_attendance(schedule_id):
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    window = _attendance_marking_window_info(schedule)
    if not window["is_open"]:
        return {
            "error": "РћРєРЅРѕ РѕС‚РјРµС‚РєРё Р·Р°РєСЂС‹С‚Рѕ.",
            "attendance_phase": window["phase"],
            "attendance_phase_message": window["message"],
            "attendance_starts_at": window["starts_at"],
            "attendance_mark_until": window["ends_at"],
        }, 403

    data = request.json or {}
    items = data.get("items") or []
    if not isinstance(items, list):
        return {"error": "items РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ СЃРїРёСЃРєРѕРј"}, 400

    staff = _get_current_staff(db)
    results = []
    now = datetime.utcnow()

    for item in items:
        user_id = item.get("user_id")
        status = (item.get("status") or "").lower()
        comment = item.get("comment")
        if status not in ATTENDANCE_ALLOWED_STATUSES:
            return {"error": f"РќРµРґРѕРїСѓСЃС‚РёРјС‹Р№ СЃС‚Р°С‚СѓСЃ: {status}"}, 400
        if not user_id:
            return {"error": "user_id РѕР±СЏР·Р°С‚РµР»РµРЅ"}, 400
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return {"error": "user_id РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј"}, 400

        att = db.query(Attendance).filter_by(schedule_id=schedule_id, user_id=user_id_int).first()
        if not att:
            att = Attendance(schedule_id=schedule_id, user_id=user_id_int)
            db.add(att)
        att.status = status
        att.comment = comment
        att.marked_at = now
        att.marked_by_staff_id = staff.id if staff else None

        if schedule.object_type == "group":
            if not att.abonement_id:
                abon = _resolve_group_active_abonement(db, user_id_int, schedule.group_id, schedule.date)
                if abon:
                    att.abonement_id = abon.id

        db.flush()
        debited = _debit_abonement_for_attendance(db, att, staff)
        results.append({
            "user_id": user_id_int,
            "status": att.status,
            "comment": att.comment,
            "abonement_id": att.abonement_id,
            "debited": debited or _attendance_already_debited(db, att.id),
        })

    db.commit()
    return {"items": results}


@bp.route("/api/attendance/<int:schedule_id>/add-user", methods=["POST"])
def add_attendance_user(schedule_id):
    db = g.db
    if not has_permission(getattr(g, "telegram_id", None) or 0, "manage_schedule"):
        return {"error": "РќРµС‚ РґРѕСЃС‚СѓРїР°"}, 403
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    window = _attendance_marking_window_info(schedule)
    if not window["is_open"]:
        return {
            "error": "РћРєРЅРѕ РѕС‚РјРµС‚РєРё Р·Р°РєСЂС‹С‚Рѕ.",
            "attendance_phase": window["phase"],
            "attendance_phase_message": window["message"],
            "attendance_starts_at": window["starts_at"],
            "attendance_mark_until": window["ends_at"],
        }, 403

    data = request.json or {}
    user_id = data.get("user_id")
    if not user_id:
        return {"error": "user_id РѕР±СЏР·Р°С‚РµР»РµРЅ"}, 400
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return {"error": "user_id РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј"}, 400

    user = db.query(User).filter_by(id=user_id_int).first()
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    existing = db.query(Attendance).filter_by(schedule_id=schedule_id, user_id=user_id_int).first()
    if existing:
        return {"message": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ СѓР¶Рµ РІ СЃРїРёСЃРєРµ"}, 200

    att = Attendance(
        schedule_id=schedule_id,
        user_id=user_id_int,
        status=data.get("status") or "absent",
        comment=data.get("comment"),
    )
    db.add(att)
    db.commit()
    return {"message": "Р”РѕР±Р°РІР»РµРЅРѕ", "user_id": user_id_int}


@bp.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["GET"])
def get_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "РќРµР»СЊР·СЏ РѕС‚РјРµС‚РёС‚СЊСЃСЏ РґР»СЏ СЌС‚РѕРіРѕ Р·Р°РЅСЏС‚РёСЏ"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()
    return _serialize_attendance_intention_with_lock(row, lock_info), 200


@bp.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["POST"])
def set_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "РќРµР»СЊР·СЏ РѕС‚РјРµС‚РёС‚СЊСЃСЏ РґР»СЏ СЌС‚РѕРіРѕ Р·Р°РЅСЏС‚РёСЏ"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    if lock_info["is_locked"]:
        return {"error": ATTENDANCE_INTENTION_LOCKED_MESSAGE, "lock": lock_info}, 403

    payload = request.json or {}
    will_miss = payload.get("will_miss")
    if will_miss is None:
        will_miss = True
    else:
        will_miss = bool(will_miss)

    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()

    if not will_miss:
        if row:
            db.delete(row)
            db.commit()
        return _serialize_attendance_intention_with_lock(None, lock_info), 200

    reason = payload.get("reason")
    if isinstance(reason, str):
        reason = reason.strip() or None
    else:
        reason = None

    if not row:
        row = AttendanceIntention(
            schedule_id=schedule_id,
            user_id=user.id,
            status=ATTENDANCE_INTENTION_STATUS_WILL_MISS,
            source="user_web",
        )
        db.add(row)

    row.status = ATTENDANCE_INTENTION_STATUS_WILL_MISS
    row.reason = reason
    row.source = "user_web"

    db.commit()
    db.refresh(row)
    return _serialize_attendance_intention_with_lock(row, lock_info), 200


@bp.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["DELETE"])
def delete_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "РќРµР»СЊР·СЏ РѕС‚РјРµС‚РёС‚СЊСЃСЏ РґР»СЏ СЌС‚РѕРіРѕ Р·Р°РЅСЏС‚РёСЏ"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    if lock_info["is_locked"]:
        return {"error": ATTENDANCE_INTENTION_LOCKED_MESSAGE, "lock": lock_info}, 403

    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()
    if row:
        db.delete(row)
        db.commit()
    return _serialize_attendance_intention_with_lock(None, lock_info), 200



