import json
import os
import re
import uuid
from datetime import date, datetime, timedelta

import requests
from flask import Blueprint, current_app, g, jsonify, request, send_from_directory
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from dance_studio.core.config import BOT_TOKEN
from dance_studio.core.media_manager import delete_user_photo
from dance_studio.core.system_settings_service import (
    SettingValidationError,
    get_setting_value,
    list_setting_changes,
    list_setting_specs,
    list_settings,
    update_setting,
)
from dance_studio.db.models import (
    Attendance,
    BookingRequest,
    Direction,
    DirectionUploadSession,
    Group,
    GroupAbonement,
    GroupAbonementActionLog,
    HallRental,
    IndividualLesson,
    Mailing,
    PaymentTransaction,
    Schedule,
    ScheduleOverrides,
    Staff,
    TeacherWorkingHours,
    User,
)
from dance_studio.web.constants import (
    ALLOWED_DIRECTION_TYPES,
    BASE_DIR,
    FRONTEND_DIR,
    INACTIVE_SCHEDULE_STATUSES,
    MEDIA_ROOT,
    PROJECT_ROOT,
)
from dance_studio.web.services.access import _get_current_staff, get_current_user_from_request, require_permission
from dance_studio.web.services.admin import (
    _append_merge_note,
    _collect_busy_intervals,
    _has_slot_conflict,
    _merge_attendance_intentions_rows,
    _merge_attendance_reminders_rows,
    _merge_attendance_rows,
    _minutes_to_time_str,
    _parse_iso_date,
    _parse_month_start,
    _parse_user_id_for_merge,
    _schedule_group_id,
    _serialize_client_abonement_for_admin,
    _subtract_busy_intervals,
    _time_to_minutes,
    format_schedule,
    format_schedule_v2,
)
from dance_studio.web.services.media import _build_image_url, normalize_teaches, try_fetch_telegram_avatar

bp = Blueprint('admin_routes', __name__)


@bp.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@bp.route("/health")
def health():
    return {"status": "ok"}


@bp.route("/bot-username")
def get_bot_username():
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ username Р±РѕС‚Р° РґР»СЏ РѕС‚РєСЂС‹С‚РёСЏ С‡Р°С‚Р°."""
    db = g.db
    try:
        configured = get_setting_value(db, "contacts.bot_username")
        db.commit()
        if isinstance(configured, str) and configured.strip():
            return jsonify({"bot_username": configured.strip().lstrip("@")})
    except Exception:
        db.rollback()
        current_app.logger.exception("Failed to resolve bot username from system settings")

    try:
        from dance_studio.bot.bot import BOT_USERNAME_GLOBAL
        if BOT_USERNAME_GLOBAL:
            return jsonify({"bot_username": str(BOT_USERNAME_GLOBAL).strip().lstrip("@")})
    except Exception:
        current_app.logger.exception("Failed to resolve runtime bot username")

    return jsonify({"bot_username": "dance_studio_admin_bot"})


@bp.route("/schedule")
def schedule():
    db = g.db
    data = db.query(Schedule).all()
    return jsonify([format_schedule(s) for s in data])


@bp.route("/schedule/public")
def schedule_public():
    db = g.db
    mine_flag = request.args.get("mine")
    user = get_current_user_from_request(db)
    mine = str(mine_flag).lower() in {"1", "true", "yes", "y"} if mine_flag is not None else bool(user)

    query = db.query(Schedule).outerjoin(IndividualLesson, Schedule.object_id == IndividualLesson.id)\
                               .outerjoin(HallRental, Schedule.object_id == HallRental.id)

    # Р±Р°Р·РѕРІС‹Р№ С„РёР»СЊС‚СЂ РїРѕ СЃС‚Р°С‚СѓСЃСѓ
    query = query.filter(Schedule.status != "cancelled")

    if mine and user:
        today = date.today()
        mine_conditions = []

        # РРЅРґРёРІРёРґСѓР°Р»СЊРЅС‹Рµ Р·Р°РЅСЏС‚РёСЏ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ
        mine_conditions.append(
            (Schedule.object_type == "individual") & (IndividualLesson.student_id == user.id)
        )
        # РђСЂРµРЅРґР°, СЃРѕР·РґР°РЅРЅР°СЏ РїРѕР»СЊР·РѕРІР°С‚РµР»РµРј
        mine_conditions.append(
            (Schedule.object_type == "rental") & (HallRental.creator_type == "user") & (HallRental.creator_id == user.id)
        )

        # Р“СЂСѓРїРїС‹ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РїРѕ Р°РєС‚РёРІРЅС‹Рј Р°Р±РѕРЅРµРјРµРЅС‚Р°Рј
        active_group_ids = [
            gid for (gid,) in db.query(GroupAbonement.group_id).filter(
                GroupAbonement.user_id == user.id,
                GroupAbonement.status == "active",
                or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= today),
                or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= today),
            ).all()
        ]
        if active_group_ids:
            mine_conditions.append(
                (Schedule.object_type == "group") & (
                    (Schedule.group_id.in_(active_group_ids)) |
                    (Schedule.object_id.in_(active_group_ids))
                )
            )

        # Р•СЃР»Рё РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ СЃРІСЏР·Р°РЅ СЃ СЃРѕС‚СЂСѓРґРЅРёРєРѕРј (РїСЂРµРїРѕРґР°РІР°С‚РµР»СЊ) вЂ” РґРѕР±Р°РІР»СЏРµРј РµРіРѕ РіСЂСѓРїРїС‹
        staff = None
        if getattr(user, "telegram_id", None):
            staff = db.query(Staff).filter_by(telegram_id=user.telegram_id).first()
        if staff:
            taught_group_ids = [gid for (gid,) in db.query(Group.id).filter(Group.teacher_id == staff.id).all()]
            staff_group_parts = [Schedule.teacher_id == staff.id]
            if taught_group_ids:
                staff_group_parts.append(Schedule.group_id.in_(taught_group_ids))
                staff_group_parts.append(Schedule.object_id.in_(taught_group_ids))
            mine_conditions.append((Schedule.object_type == "group") & or_(*staff_group_parts))

        if mine_conditions:
            query = query.filter(or_(*mine_conditions))
        else:
            query = query.filter(Schedule.id == -1)
    else:
        # РїСѓР±Р»РёС‡РЅР°СЏ РІС‹РґР°С‡Р° С‚РѕР»СЊРєРѕ РіСЂСѓРїРї
        query = query.filter(Schedule.object_type == "group")

    items = query.all()

    result = []
    for s in items:
        time_from = s.time_from or s.start_time
        time_to = s.time_to or s.end_time

        entry = {
            "id": s.id,
            "object_type": s.object_type,
            "date": s.date.isoformat() if s.date else None,
            "start": str(time_from) if time_from else None,
            "end": str(time_to) if time_to else None,
        }

        if s.object_type == "group":
            group = None
            if s.group_id:
                group = db.query(Group).filter_by(id=s.group_id).first()
            elif s.object_id:
                group = db.query(Group).filter_by(id=s.object_id).first()

            direction_title = None
            direction_description = None
            direction_image = None
            direction_id = None
            teacher_name = None
            lessons_per_week = None
            age_group = None
            if group:
                if group.direction_id:
                    direction_id = group.direction_id
                    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first()
                    if direction:
                        direction_title = direction.title
                        direction_description = direction.description
                        direction_image = _build_image_url(direction.image_path)
                if group.teacher_id:
                    teacher = db.query(Staff).filter_by(id=group.teacher_id).first()
                    teacher_name = teacher.name if teacher else None
                lessons_per_week = group.lessons_per_week
                age_group = group.age_group

            entry.update({
                "title": group.name if group and group.name else s.title,
                "direction": direction_title,
                "direction_description": direction_description,
                "direction_image": direction_image,
                "direction_id": direction_id,
                "teacher_name": teacher_name,
                "lessons_per_week": lessons_per_week,
                "age_group": age_group,
            })
        elif s.object_type == "individual":
            lesson = db.query(IndividualLesson).filter_by(id=s.object_id).first() if s.object_id else None
            teacher = db.query(Staff).filter_by(id=s.teacher_id).first() if s.teacher_id else None
            entry.update({
                "title": s.title or "РРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРµ Р·Р°РЅСЏС‚РёРµ",
                "teacher_name": teacher.name if teacher else None,
                "student_id": lesson.student_id if lesson else None,
                "status": s.status,
            })
        elif s.object_type == "rental":
            rental = db.query(HallRental).filter_by(id=s.object_id).first() if s.object_id else None
            entry.update({
                "title": s.title or "РђСЂРµРЅРґР° Р·Р°Р»Р°",
                "creator_id": rental.creator_id if rental else None,
                "creator_type": rental.creator_type if rental else None,
                "status": s.status,
            })
        else:
            entry["title"] = s.title

        result.append(entry)

    return jsonify(result)


@bp.route("/schedule/v2", methods=["GET"])
def schedule_v2_list():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    query = db.query(Schedule)
    object_type = request.args.get("object_type")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    mine_flag = request.args.get("mine")
    mine = str(mine_flag).lower() in {"1", "true", "yes", "y"} if mine_flag is not None else False

    if object_type:
        query = query.filter(Schedule.object_type == object_type)
    if date_from:
        try:
            date_from_val = datetime.strptime(date_from, "%Y-%m-%d").date()
            query = query.filter(Schedule.date >= date_from_val)
        except ValueError:
            return {"error": "date_from РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD"}, 400
    if date_to:
        try:
            date_to_val = datetime.strptime(date_to, "%Y-%m-%d").date()
            query = query.filter(Schedule.date <= date_to_val)
        except ValueError:
            return {"error": "date_to РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD"}, 400

    if mine:
        user = get_current_user_from_request(db)
        if not user:
            return {"error": "РўСЂРµР±СѓРµС‚СЃСЏ Р°РІС‚РѕСЂРёР·Р°С†РёСЏ"}, 401
        query = query.outerjoin(IndividualLesson, Schedule.object_id == IndividualLesson.id)\
                     .outerjoin(HallRental, Schedule.object_id == HallRental.id)\
                     .filter(
                         or_(
                             (Schedule.object_type == "individual") & (IndividualLesson.student_id == user.id),
                             (Schedule.object_type == "rental") & (HallRental.creator_type == "user") & (HallRental.creator_id == user.id)
                         )
                     )

    data = query.all()
    return jsonify([format_schedule_v2(s) for s in data])


@bp.route("/schedule", methods=["POST"])
def create_schedule():
    """
    РЎРѕР·РґР°РµС‚ РЅРѕРІРѕРµ Р·Р°РЅСЏС‚РёРµ
    """
    db = g.db
    data = request.json or {}

    

    if not data.get("title") or not data.get("teacher_id") or not data.get("date") or not data.get("start_time") or not data.get("end_time"):
        return {"error": "title, teacher_id, date, start_time Рё end_time РѕР±СЏР·Р°С‚РµР»СЊРЅС‹"}, 400
    
    teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
    if not teacher:
        return {"error": "РЈС‡РёС‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    schedule = Schedule(
        title=data["title"],
        teacher_id=data["teacher_id"],
        date=datetime.strptime(data["date"], "%Y-%m-%d").date(),
        start_time=datetime.strptime(data["start_time"], "%H:%M").time(),
        end_time=datetime.strptime(data["end_time"], "%H:%M").time()
    )
    db.add(schedule)
    db.commit()
    
    return format_schedule(schedule), 201


@bp.route("/schedule/v2", methods=["POST"])
def create_schedule_v2():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}

    object_type = data.get("object_type")
    object_id = data.get("object_id")
    date_str = data.get("date")
    time_from_str = data.get("time_from")
    time_to_str = data.get("time_to")
    repeat_until_str = data.get("repeat_weekly_until")

    if object_type not in ["group", "individual", "rental"]:
        return {"error": "object_type РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РѕРґРЅРёРј РёР·: group, individual, rental"}, 400
    if not object_id:
        return {"error": "object_id РѕР±СЏР·Р°С‚РµР»РµРЅ"}, 400
    if not date_str or not time_from_str or not time_to_str:
        return {"error": "date, time_from, time_to РѕР±СЏР·Р°С‚РµР»СЊРЅС‹"}, 400

    try:
        date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
        time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
    except ValueError:
        return {"error": "РќРµРІРµСЂРЅС‹Р№ С„РѕСЂРјР°С‚ РґР°С‚С‹ РёР»Рё РІСЂРµРјРµРЅРё"}, 400

    if time_from_val >= time_to_val:
        return {"error": "time_from РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РјРµРЅСЊС€Рµ time_to"}, 400

    group_id = data.get("group_id")
    teacher_id = data.get("teacher_id")

    title = None
    if object_type == "group":
        group = db.query(Group).filter_by(id=object_id).first()
        if not group:
            return {"error": "Р“СЂСѓРїРїР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404
        group_id = group.id
        teacher_id = group.teacher_id
        title = group.name
    elif object_type == "individual":
        lesson = db.query(IndividualLesson).filter_by(id=object_id).first()
        if not lesson:
            return {"error": "РРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРµ Р·Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
        teacher_id = lesson.teacher_id
        title = "РРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРµ Р·Р°РЅСЏС‚РёРµ"
    elif object_type == "rental":
        rental = db.query(HallRental).filter_by(id=object_id).first()
        if not rental:
            return {"error": "РђСЂРµРЅРґР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404
        title = "РђСЂРµРЅРґР° Р·Р°Р»Р°"

    def build_entry(entry_date):
        return Schedule(
            object_id=object_id,
            object_type=object_type,
            date=entry_date,
            time_from=time_from_val,
            time_to=time_to_val,
            status=data.get("status", "scheduled"),
            status_comment=data.get("status_comment"),
            updated_by=data.get("updated_by"),
            group_id=group_id,
            teacher_id=teacher_id,
            title=title or f"{object_type} #{object_id}",
            start_time=time_from_val,
            end_time=time_to_val
        )

    entries = [build_entry(date_val)]
    if repeat_until_str:
        try:
            repeat_until = datetime.strptime(repeat_until_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "repeat_weekly_until РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD"}, 400
        current_date = date_val
        while True:
            current_date = current_date + timedelta(days=7)
            if current_date > repeat_until:
                break
            entries.append(build_entry(current_date))

    for entry in entries:
        db.add(entry)
    db.commit()

    return jsonify([format_schedule_v2(s) for s in entries]), 201


@bp.route("/schedule/<int:schedule_id>", methods=["PUT"])
def update_schedule(schedule_id):
    """
    РћР±РЅРѕРІР»СЏРµС‚ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµРµ Р·Р°РЅСЏС‚РёРµ
    """
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    
    data = request.json
    
    if data.get("title"):
        schedule.title = data["title"]
    if data.get("teacher_id"):
        teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
        if not teacher:
            return {"error": "РЈС‡РёС‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404
        schedule.teacher_id = data["teacher_id"]
    if data.get("date"):
        schedule.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    if data.get("start_time"):
        schedule.start_time = datetime.strptime(data["start_time"], "%H:%M").time()
    if data.get("end_time"):
        schedule.end_time = datetime.strptime(data["end_time"], "%H:%M").time()
    
    db.commit()
    
    return format_schedule(schedule)


@bp.route("/schedule/v2/<int:schedule_id>", methods=["PUT"])
def update_schedule_v2(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    data = request.json or {}

    if "date" in data:
        try:
            schedule.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD Рё Р±С‹С‚СЊ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµР№ РґР°С‚РѕР№"}, 400
    if "time_from" in data:
        try:
            schedule.time_from = datetime.strptime(data["time_from"], "%H:%M").time()
        except ValueError:
            return {"error": "time_from РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ HH:MM"}, 400
    if "time_to" in data:
        try:
            schedule.time_to = datetime.strptime(data["time_to"], "%H:%M").time()
        except ValueError:
            return {"error": "time_to РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ HH:MM"}, 400
    if "status" in data:
        schedule.status = data["status"]
    if "status_comment" in data:
        schedule.status_comment = data["status_comment"]
    if "updated_by" in data:
        schedule.updated_by = data["updated_by"]

    db.commit()
    return format_schedule_v2(schedule)


@bp.route("/schedule/<int:schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    """
    РЈРґР°Р»СЏРµС‚ Р·Р°РЅСЏС‚РёРµ
    """
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Р—Р°РЅСЏС‚РёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    
    schedule.status = "cancelled"
    schedule.status_comment = schedule.status_comment or "РћС‚РјРµРЅРµРЅРѕ"
    db.commit()

    return {"ok": True, "message": "Р—Р°РЅСЏС‚РёРµ РѕС‚РјРµРЅРµРЅРѕ"}


@bp.route("/schedule/v2/<int:schedule_id>", methods=["DELETE"])


# -------------------- ATTENDANCE --------------------

def _resolve_group_active_abonement(db, user_id: int, group_id: int, date_val):
    if not group_id:
        return None
    query = db.query(GroupAbonement).filter(
        GroupAbonement.user_id == user_id,
        GroupAbonement.group_id == group_id,
        GroupAbonement.status == "active",
    )
    if date_val:
        query = query.filter(
            or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= date_val),
            or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= date_val),
        )
    return query.order_by(GroupAbonement.valid_to.is_(None), GroupAbonement.valid_to).first()


@bp.route("/users", methods=["POST"])
def register_user():
    db = g.db
    data = request.json or {}

    if not data.get("name"):
        return {"error": "name is required"}, 400

    telegram_id_raw = data.get("telegram_id")
    telegram_id = None
    if telegram_id_raw not in (None, ""):
        try:
            telegram_id = int(telegram_id_raw)
        except (TypeError, ValueError):
            return {"error": "telegram_id must be an integer"}, 400

    if telegram_id is not None:
        existing_user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if existing_user:
            return {"error": "user with this telegram_id already exists"}, 409

    user = User(
        telegram_id=telegram_id,
        username=data.get("username"),
        phone=data.get("phone"),
        name=data["name"],
        email=data.get("email"),
        birth_date=datetime.strptime(data["birth_date"], "%Y-%m-%d").date() if data.get("birth_date") else None,
        user_notes=data.get("user_notes"),
        staff_notes=data.get("staff_notes")
    )
    db.add(user)
    db.commit()

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes
    }, 201


@bp.route("/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes
    }


@bp.route("/users/me", methods=["GET"])
def get_my_user():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "user not found"}, 404
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes,
        "photo_path": user.photo_path,
    }


@bp.route("/users/list/all")
def list_all_users():
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error
    db = g.db
    users = db.query(User).order_by(User.registered_at.desc()).all()
    
    return jsonify([
        {
            "id": u.id,
            "telegram_id": u.telegram_id,
            "username": u.username,
            "phone": u.phone,
            "name": u.name,
            "email": u.email,
            "birth_date": u.birth_date.isoformat() if u.birth_date else None,
            "registered_at": u.registered_at.isoformat(),
            "status": u.status,
            "user_notes": u.user_notes,
            "staff_notes": u.staff_notes
        } for u in users
    ])


@bp.route("/users/<int:user_id>", methods=["PUT"])
def update_user(user_id):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    
    if not user:
        return {"error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    data = request.json or {}
    
    if "phone" in data:
        user.phone = data["phone"]
    if "name" in data:
        user.name = data["name"]
    if "email" in data:
        user.email = data["email"]
    if "birth_date" in data and data["birth_date"]:
        user.birth_date = datetime.strptime(data["birth_date"], "%Y-%m-%d").date()
    if "status" in data:
        user.status = data["status"]
    if "user_notes" in data:
        user.user_notes = data["user_notes"]
    if "staff_notes" in data:
        user.staff_notes = data["staff_notes"]
    
    db.commit()
    
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "phone": user.phone,
        "name": user.name,
        "email": user.email,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "registered_at": user.registered_at.isoformat(),
        "status": user.status,
        "user_notes": user.user_notes,
        "staff_notes": user.staff_notes,
        "photo_path": user.photo_path
    }


@bp.route("/staff")
def get_all_staff():
    """
    РџРѕР»СѓС‡РёС‚СЊ РІСЃРµС… СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ
    """
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter_by(status="active").order_by(Staff.created_at.desc()).all()
    
    result = []
    for s in staff:
        # РџРѕР»СѓС‡Р°РµРј username РёР· User РµСЃР»Рё РµСЃС‚СЊ telegram_id
        username = None
        if s.telegram_id:
            user = db.query(User).filter_by(telegram_id=s.telegram_id).first()
            if user:
                username = user.username
        
        result.append({
            "id": s.id,
            "name": s.name,
            "phone": s.phone,
            "email": s.email,
            "telegram_id": s.telegram_id,
            "username": username,
            "position": s.position,
            "specialization": s.specialization,
            "bio": s.bio,
            "photo_path": s.photo_path,
            "teaches": s.teaches,
            "status": s.status,
            "created_at": s.created_at.isoformat()
        })
    
    return jsonify(result)


@bp.route("/staff/check/<int:telegram_id>")
def check_staff_by_telegram(telegram_id):
    """
    РџСЂРѕРІРµСЂРёС‚СЊ СЏРІР»СЏРµС‚СЃСЏ Р»Рё РїРѕР»СЊР·РѕРІР°С‚РµР»РµРј СЃРѕС‚СЂСѓРґРЅРёРєРѕРј.
    Р•СЃР»Рё РґР°РЅРЅС‹Рµ РїРµСЂСЃРѕРЅР°Р»Р° РЅРµРїРѕР»РЅС‹Рµ, РїРѕРґРіСЂСѓР¶Р°РµС‚ РґР°РЅРЅС‹Рµ РёР· РїСЂРѕС„РёР»СЏ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ (Р±РµР· СЃРѕС…СЂР°РЅРµРЅРёСЏ РІ Р‘Р”).
    """
    try:
        db = g.db
        staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
        
        if not staff:
            return jsonify({
                "is_staff": False,
                "staff": None
            })
        
        # Р—Р°РіСЂСѓР¶Р°РµРј РїСЂРѕС„РёР»СЊ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РґР»СЏ РїРѕРґСЃС‚Р°РЅРѕРІРєРё РґР°РЅРЅС‹С…
        try:
            user = db.query(User).filter_by(telegram_id=telegram_id).first()
        except:
            user = None
        
        # Р•СЃР»Рё РґР°РЅРЅС‹Рµ РїРµСЂСЃРѕРЅР°Р»Р° РЅРµРїРѕР»РЅС‹Рµ, Р±РµСЂРµРј РёР· РїСЂРѕС„РёР»СЏ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ
        staff_data = {
            "id": staff.id,
            "name": staff.name or (user.name if user else None),
            "position": staff.position,
            "specialization": staff.specialization,
            "bio": staff.bio,
            "teaches": staff.teaches,
            "phone": staff.phone,
            "email": staff.email,
            "photo_path": staff.photo_path or (user.photo_path if user else None)
        }
        
        return jsonify({
            "is_staff": True,
            "staff": staff_data
        })
    except Exception as e:
        print(f"вљ пёЏ РћС€РёР±РєР° РїСЂРё РїСЂРѕРІРµСЂРєРµ СЃРѕС‚СЂСѓРґРЅРёРєР°: {e}")
        return jsonify({
            "is_staff": False,
            "staff": None
        })


@bp.route("/staff", methods=["POST"])
def create_staff():
    """
    РЎРѕР·РґР°С‚СЊ РЅРѕРІС‹Р№ РїСЂРѕС„РёР»СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°.
    РћР±СЏР·Р°С‚РµР»СЊРЅС‹Рµ РїРѕР»СЏ: position, name (РёР»Рё telegram_id СЃ РїСЂРѕС„РёР»РµРј)
    РћСЃС‚Р°Р»СЊРЅС‹Рµ РѕРїС†РёРѕРЅР°Р»СЊРЅС‹Рµ.
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    # РџРѕР»СѓС‡Р°РµРј РёРјСЏ: Р»РёР±Рѕ РёР· РґР°РЅРЅС‹С…, Р»РёР±Рѕ РёР· РїСЂРѕС„РёР»СЏ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ
    staff_name = data.get("name")
    if not staff_name and data.get("telegram_id"):
        user = db.query(User).filter_by(telegram_id=data.get("telegram_id")).first()
        if user and user.name:
            staff_name = user.name
    
    if not staff_name or not data.get("position"):
        return {"error": "name (РёР»Рё telegram_id СЃ РїСЂРѕС„РёР»РµРј) Рё position РѕР±СЏР·Р°С‚РµР»СЊРЅС‹"}, 400

    # РџСЂРѕРІРµСЂСЏРµРј РґРѕРїСѓСЃС‚РёРјС‹Рµ РґРѕР»Р¶РЅРѕСЃС‚Рё
    valid_positions = ["СѓС‡РёС‚РµР»СЊ", "Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ", "СЃС‚Р°СЂС€РёР№ Р°РґРјРёРЅ", "РІР»Р°РґРµР»РµС†", "С‚РµС…. Р°РґРјРёРЅ"]
    if data.get("position").lower() not in valid_positions:
        return {"error": f"Р”РѕРїСѓСЃС‚РёРјС‹Рµ РґРѕР»Р¶РЅРѕСЃС‚Рё: {', '.join(valid_positions)}"}, 400

    notify_flag = data.get("notify", True)
    notify_user = str(notify_flag).strip().lower() in ["1", "true", "yes", "y", "on"]

    teaches_value = 0
    teaches_raw = normalize_teaches(data.get("teaches"))
    if teaches_raw is None:
        teaches_value = 1 if data.get("position").lower() == "СѓС‡РёС‚РµР»СЊ" else 0
    else:
        teaches_value = teaches_raw

    
    # Р—Р°С‰РёС‚Р° РѕС‚ РґСѓР±Р»РµР№ РїРѕ telegram_id
    if data.get("telegram_id"):
        existing_staff = db.query(Staff).filter_by(telegram_id=data.get("telegram_id")).first()
        if existing_staff:
            if existing_staff.status == "dismissed":
                existing_staff.name = staff_name
                existing_staff.position = data["position"]
                existing_staff.specialization = data.get("specialization")
                existing_staff.bio = data.get("bio")
                existing_staff.status = "active"
                existing_staff.teaches = teaches_value
                db.commit()

                if data.get("telegram_id"):
                    try_fetch_telegram_avatar(data.get("telegram_id"), db, staff_obj=existing_staff)

                if data.get("telegram_id") and notify_user:
                    try:
                        import requests
                        from dance_studio.core.config import BOT_TOKEN

                        position_display = {
                            "СѓС‡РёС‚РµР»СЊ": "рџ‘©вЂЌрџЏ« РЈС‡РёС‚РµР»СЊ",
                            "Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ": "рџ“‹ РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ",
                            "СЃС‚Р°СЂС€РёР№ Р°РґРјРёРЅ": "рџ›ЎпёЏ РЎС‚Р°СЂС€РёР№ Р°РґРјРёРЅ",
                            "РІР»Р°РґРµР»РµС†": "рџ‘‘ Р’Р»Р°РґРµР»РµС†",
                            "С‚РµС…. Р°РґРјРёРЅ": "вљ™пёЏ РўРµС…РЅРёС‡РµСЃРєРёР№ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ"
                        }

                        position_name = position_display.get(data["position"], data["position"])
                        message_text = (
                            f"рџЋ‰ Р’С‹ СЃРЅРѕРІР° РІ РєРѕРјР°РЅРґРµ!\n\n"
                            f"Р’Р°Рј РЅР°Р·РЅР°С‡РµРЅР° РґРѕР»Р¶РЅРѕСЃС‚СЊ:\n"
                            f"<b>{position_name}</b>\n\n"
                            f"Р”РѕР±СЂРѕ РїРѕР¶Р°Р»РѕРІР°С‚СЊ РѕР±СЂР°С‚РЅРѕ!"
                        )

                        telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                        payload = {
                            "chat_id": data.get("telegram_id"),
                            "text": message_text,
                            "parse_mode": "HTML"
                        }
                        requests.post(telegram_api_url, json=payload, timeout=5)
                    except Exception:
                        pass

                return {
                    "message": "РџРµСЂСЃРѕРЅР°Р» РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅ",
                    "id": existing_staff.id,
                    "restored": True
                }, 200

            return {
                "error": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ СЃ С‚Р°РєРёРј telegram_id СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚",
                "existing_id": existing_staff.id
            }, 409
    
    staff = Staff(
        name=staff_name,
        phone=data.get("phone") or "+7 000 000 00 00",  # РўРµР»РµС„РѕРЅ РѕРїС†РёРѕРЅР°Р»СЊРЅС‹Р№
        email=data.get("email"),
        telegram_id=data.get("telegram_id"),
        position=data["position"],
        specialization=data.get("specialization"),
        bio=data.get("bio"),
        teaches=teaches_value,
        status=data.get("status", "active")
    )
    db.add(staff)
    db.commit()

    if data.get("telegram_id"):
        try_fetch_telegram_avatar(data.get("telegram_id"), db, staff_obj=staff)
    
    # РћС‚РїСЂР°РІР»СЏРµРј СѓРІРµРґРѕРјР»РµРЅРёРµ РІ Telegram РµСЃР»Рё РµСЃС‚СЊ telegram_id
    if data.get("telegram_id") and notify_user:
        try:
            import requests
            from dance_studio.core.config import BOT_TOKEN
            
            position_display = {
                "СѓС‡РёС‚РµР»СЊ": "рџ‘©вЂЌрџЏ« РЈС‡РёС‚РµР»СЊ",
                "Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ": "рџ“‹ РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ",
                "СЃС‚Р°СЂС€РёР№ Р°РґРјРёРЅ": "рџ›ЎпёЏ РЎС‚Р°СЂС€РёР№ Р°РґРјРёРЅ",
                "РІР»Р°РґРµР»РµС†": "рџ‘‘ Р’Р»Р°РґРµР»РµС†",
                "С‚РµС…. Р°РґРјРёРЅ": "вљ™пёЏ РўРµС…РЅРёС‡РµСЃРєРёР№ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ"
            }
            
            position_name = position_display.get(data["position"], data["position"])
            
            message_text = (
                f"рџЋ‰ РџРѕР·РґСЂР°РІР»СЏРµРј!\n\n"
                f"Р’С‹ РЅР°Р·РЅР°С‡РµРЅС‹ РЅР° РґРѕР»Р¶РЅРѕСЃС‚СЊ:\n"
                f"<b>{position_name}</b>\n\n"
                f"РІ СЃС‚СѓРґРёРё С‚Р°РЅС†Р° LISSA DANCE!"
            )
            
            # РћС‚РїСЂР°РІР»СЏРµРј СЃРѕРѕР±С‰РµРЅРёРµ РЅР°РїСЂСЏРјСѓСЋ С‡РµСЂРµР· Telegram API
            telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": data.get("telegram_id"),
                "text": message_text,
                "parse_mode": "HTML"
            }
            
            response = requests.post(telegram_api_url, json=payload, timeout=5)
            if response.status_code == 200:
                pass  # print(f"вњ… РЈРІРµРґРѕРјР»РµРЅРёРµ РѕС‚РїСЂР°РІР»РµРЅРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЋ {data.get('telegram_id')}")
            else:
                pass  # print(f"вљ пёЏ РћС€РёР±РєР° РїСЂРё РѕС‚РїСЂР°РІРєРµ СѓРІРµРґРѕРјР»РµРЅРёСЏ: {response.text}")
                
        except Exception as e:
            pass  # print(f"вљ пёЏ РћС€РёР±РєР° РїСЂРё РѕС‚РїСЂР°РІРєРµ СѓРІРµРґРѕРјР»РµРЅРёСЏ: {e}")
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": staff.photo_path,
        "status": staff.status,
        "created_at": staff.created_at.isoformat()
    }, 201


@bp.route("/staff/<int:staff_id>", methods=["GET"])
def get_staff(staff_id):
    """
    РџРѕР»СѓС‡РёС‚СЊ РёРЅС„РѕСЂРјР°С†РёСЋ Рѕ СЃРѕС‚СЂСѓРґРЅРёРєРµ
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "РЎРѕС‚СЂСѓРґРЅРёРє РЅРµ РЅР°Р№РґРµРЅ"}, 404

    username = None
    photo_path = staff.photo_path
    if staff.telegram_id:
        user = db.query(User).filter_by(telegram_id=staff.telegram_id).first()
        if user:
            username = user.username
            if not photo_path and user.photo_path:
                photo_path = user.photo_path
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "username": username,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": photo_path,
        "teaches": staff.teaches,
        "status": staff.status,
        "created_at": staff.created_at.isoformat()
    }


@bp.route("/staff/update-from-telegram/<int:telegram_id>", methods=["PUT"])
def update_staff_from_telegram(telegram_id):
    """
    РћР±РЅРѕРІР»СЏРµС‚ РёРјСЏ Рё РґСЂСѓРіРёРµ РґР°РЅРЅС‹Рµ РїРµСЂСЃРѕРЅР°Р»Р° РёР· Telegram РїСЂРѕС„РёР»СЏ
    """
    db = g.db
    data = request.json
    
    staff = db.query(Staff).filter_by(telegram_id=telegram_id).first()
    
    if not staff:
        return {"error": "РџРµСЂСЃРѕРЅР°Р» РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    if "first_name" in data:
        # Р¤РѕСЂРјРёСЂСѓРµРј РїРѕР»РЅРѕРµ РёРјСЏ РёР· first_name Рё last_name
        name = data["first_name"]
        if data.get("last_name"):
            name += " " + data["last_name"]
        staff.name = name
    
    db.commit()
    
    return {
        "id": staff.id,
        "name": staff.name,
        "position": staff.position,
        "message": "РјСЏ РѕР±РЅРѕРІР»РµРЅРѕ РёР· Telegram"
    }


@bp.route("/staff/<int:staff_id>", methods=["PUT"])
def update_staff(staff_id):
    """
    РћР±РЅРѕРІРёС‚СЊ РёРЅС„РѕСЂРјР°С†РёСЋ Рѕ СЃРѕС‚СЂСѓРґРЅРёРєРµ
    """
    perm_error = require_permission("manage_staff", allow_self_staff_id=staff_id)
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "РЎРѕС‚СЂСѓРґРЅРёРє РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    data = request.json
    
    if "name" in data:
        staff.name = data["name"]
    if "phone" in data:
        staff.phone = data["phone"]
    if "email" in data:
        staff.email = data["email"]
    if "telegram_id" in data:
        staff.telegram_id = data["telegram_id"]
    if "position" in data:
        valid_positions = {"СѓС‡РёС‚РµР»СЊ", "Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ", "СЃС‚Р°СЂС€РёР№ Р°РґРјРёРЅ", "РјРѕРґРµСЂР°С‚РѕСЂ", "РІР»Р°РґРµР»РµС†", "С‚РµС…. Р°РґРјРёРЅ"}
        normalized_position = str(data["position"]).strip().lower()
        if normalized_position not in valid_positions:
            return {"error": f"Р”РѕРїСѓСЃС‚РёРјС‹Рµ РґРѕР»Р¶РЅРѕСЃС‚Рё: {', '.join(valid_positions)}"}, 400
        staff.position = normalized_position
    if "specialization" in data:
        staff.specialization = data["specialization"]
    if "bio" in data:
        staff.bio = data["bio"]
    if "teaches" in data:
        actor_telegram_id = getattr(g, "telegram_id", None)
        try:
            actor_telegram_id = int(actor_telegram_id) if actor_telegram_id is not None else None
        except (TypeError, ValueError):
            return {"error": "РќРµРІРµСЂРЅС‹Р№ telegram_id"}, 400
        actor_staff = None
        if actor_telegram_id is not None:
            actor_staff = db.query(Staff).filter_by(telegram_id=actor_telegram_id, status="active").first()
        allowed_positions = {"Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ", "СЃС‚Р°СЂС€РёР№ Р°РґРјРёРЅ", "РІР»Р°РґРµР»РµС†", "С‚РµС…. Р°РґРјРёРЅ"}
        actor_position = (actor_staff.position or "").strip().lower() if actor_staff else ""
        if actor_position not in allowed_positions:
            return {"error": "РќРµС‚ РїСЂР°РІ РЅР° РёР·РјРµРЅРµРЅРёРµ РїРѕР»СЏ teaches"}, 403
        staff.teaches = normalize_teaches(data["teaches"])
    if "status" in data:
        staff.status = data["status"]
    
    db.commit()
    
    return {
        "id": staff.id,
        "name": staff.name,
        "phone": staff.phone,
        "email": staff.email,
        "telegram_id": staff.telegram_id,
        "position": staff.position,
        "specialization": staff.specialization,
        "bio": staff.bio,
        "photo_path": staff.photo_path,
        "teaches": staff.teaches,
        "status": staff.status,
        "created_at": staff.created_at.isoformat()
    }


@bp.route("/teacher-working-hours/<int:teacher_id>", methods=["GET"])
def get_teacher_working_hours(teacher_id):
    perm_error = require_permission("manage_staff", allow_self_staff_id=teacher_id)
    if perm_error:
        return perm_error

    db = g.db
    items = (
        db.query(TeacherWorkingHours)
        .filter_by(teacher_id=teacher_id, status="active")
        .order_by(TeacherWorkingHours.weekday.asc(), TeacherWorkingHours.time_from.asc())
        .all()
    )
    return [
        {
            "id": i.id,
            "teacher_id": i.teacher_id,
            "weekday": i.weekday,
            "time_from": i.time_from.strftime("%H:%M") if i.time_from else None,
            "time_to": i.time_to.strftime("%H:%M") if i.time_to else None,
            "valid_from": i.valid_from.isoformat() if i.valid_from else None,
            "valid_to": i.valid_to.isoformat() if i.valid_to else None,
            "status": i.status,
            "created_at": i.created_at.isoformat() if i.created_at else None,
            "updated_at": i.updated_at.isoformat() if i.updated_at else None
        }
        for i in items
    ]


@bp.route("/api/stats/teacher", methods=["GET"])
def get_teacher_stats():
    perm_error = require_permission("view_stats")
    if perm_error:
        return perm_error

    db = g.db
    try:
        teacher_id = int(request.args.get("teacher_id", 0))
    except (TypeError, ValueError):
        return {"error": "teacher_id РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј"}, 400
    if not teacher_id:
        return {"error": "teacher_id РѕР±СЏР·Р°С‚РµР»РµРЅ"}, 400

    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    try:
        date_from_val = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        date_to_val = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
    except ValueError:
        return {"error": "РќРµРІРµСЂРЅС‹Р№ С„РѕСЂРјР°С‚ РґР°С‚С‹, РёСЃРїРѕР»СЊР·СѓР№С‚Рµ YYYY-MM-DD"}, 400

    schedules_q = db.query(Schedule).filter(
        Schedule.teacher_id == teacher_id,
        Schedule.status != "cancelled"
    )
    if date_from_val:
        schedules_q = schedules_q.filter(Schedule.date >= date_from_val)
    if date_to_val:
        schedules_q = schedules_q.filter(Schedule.date <= date_to_val)

    schedules = schedules_q.all()
    schedule_ids = [s.id for s in schedules]

    stats = {
        "teacher_id": teacher_id,
        "date_from": date_from,
        "date_to": date_to,
        "lessons_count": len(schedules),
        "students_total": 0,
        "present": 0,
        "absent": 0,
        "late": 0,
        "sick": 0,
    }

    if schedule_ids:
        attendance_rows = db.query(Attendance).filter(Attendance.schedule_id.in_(schedule_ids)).all()
        for row in attendance_rows:
            status = row.status or "absent"
            if status == "sick":
                stats["sick"] += 1
                continue
            stats["students_total"] += 1
            if status == "present":
                stats["present"] += 1
            elif status == "late":
                stats["late"] += 1
            else:
                stats["absent"] += 1

    return jsonify(stats)


@bp.route("/teacher-working-hours/<int:teacher_id>", methods=["PUT"])
def put_teacher_working_hours(teacher_id):
    perm_error = require_permission("manage_staff", allow_self_staff_id=teacher_id)
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}
    items = data.get("items", [])
    if not isinstance(items, list):
        return {"error": "items РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ СЃРїРёСЃРєРѕРј"}, 400

    parsed_items = []
    for item in items:
        try:
            weekday = int(item.get("weekday"))
        except (TypeError, ValueError):
            return {"error": "weekday РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј 0..6"}, 400
        if weekday < 0 or weekday > 6:
            return {"error": "weekday РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ РґРёР°РїР°Р·РѕРЅРµ 0..6"}, 400

        time_from_str = item.get("time_from")
        time_to_str = item.get("time_to")
        if not time_from_str or not time_to_str:
            return {"error": "time_from Рё time_to РѕР±СЏР·Р°С‚РµР»СЊРЅС‹"}, 400
        try:
            time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
            time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
        except ValueError:
            return {"error": "time_from Рё time_to РґРѕР»Р¶РЅС‹ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ HH:MM"}, 400
        if time_from_val >= time_to_val:
            return {"error": "time_from РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РјРµРЅСЊС€Рµ time_to"}, 400

        valid_from = item.get("valid_from")
        valid_to = item.get("valid_to")
        try:
            valid_from_val = datetime.strptime(valid_from, "%Y-%m-%d").date() if valid_from else None
            valid_to_val = datetime.strptime(valid_to, "%Y-%m-%d").date() if valid_to else None
        except ValueError:
            return {"error": "valid_from Рё valid_to РґРѕР»Р¶РЅС‹ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD"}, 400

        parsed_items.append(
            {
                "weekday": weekday,
                "time_from": time_from_val,
                "time_to": time_to_val,
                "valid_from": valid_from_val,
                "valid_to": valid_to_val,
            }
        )

    existing = db.query(TeacherWorkingHours).filter_by(teacher_id=teacher_id, status="active").all()
    for row in existing:
        row.status = "archived"
        row.updated_at = datetime.now()

    for item in parsed_items:
        db.add(
            TeacherWorkingHours(
                teacher_id=teacher_id,
                weekday=item["weekday"],
                time_from=item["time_from"],
                time_to=item["time_to"],
                valid_from=item["valid_from"],
                valid_to=item["valid_to"],
                status="active",
            )
        )

    db.commit()

    return {
        "items": [
            {
                "weekday": i["weekday"],
                "time_from": i["time_from"].strftime("%H:%M"),
                "time_to": i["time_to"].strftime("%H:%M"),
                "valid_from": i["valid_from"].isoformat() if i["valid_from"] else None,
                "valid_to": i["valid_to"].isoformat() if i["valid_to"] else None,
                "status": "active",
            }
            for i in parsed_items
        ]
    }


@bp.route("/staff/<int:staff_id>", methods=["DELETE"])
def delete_staff(staff_id):
    """
    РЈРґР°Р»РёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "РЎРѕС‚СЂСѓРґРЅРёРє РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    staff_name = staff.name
    telegram_id = staff.telegram_id

    # Р’РјРµСЃС‚Рѕ С„РёР·РёС‡РµСЃРєРѕРіРѕ СѓРґР°Р»РµРЅРёСЏ вЂ” РґРµР°РєС‚РёРІРёСЂСѓРµРј, С‡С‚РѕР±С‹ РЅРµ Р»РѕРјР°С‚СЊ СЂР°СЃРїРёСЃР°РЅРёРµ
    staff.status = "dismissed"
    staff.teaches = 0
    db.commit()
    
    notify_flag = request.args.get("notify", "1").strip().lower()
    notify_user = notify_flag in ["1", "true", "yes", "y", "on"]

    # РћС‚РїСЂР°РІР»СЏРµРј СѓРІРµРґРѕРјР»РµРЅРёРµ РѕР± СѓРІРѕР»СЊРЅРµРЅРёРё РІ Telegram РµСЃР»Рё РµСЃС‚СЊ telegram_id
    if telegram_id and notify_user:
        try:
            import requests
            from dance_studio.core.config import BOT_TOKEN
            
            message_text = (
                f" Рљ СЃРѕР¶Р°Р»РµРЅРёСЋ...\n\n"
                f"Р’С‹ СѓРґР°Р»РµРЅС‹ РёР· РїРµСЂСЃРѕРЅР°Р»Р° СЃС‚СѓРґРёРё С‚Р°РЅС†Р° LISSA DANCE.\n\n"
                f"РЎРїР°СЃРёР±Рѕ Р·Р° СЃРѕС‚СЂСѓРґРЅРёС‡РµСЃС‚РІРѕ!"
            )
            
            # РћС‚РїСЂР°РІР»СЏРµРј СЃРѕРѕР±С‰РµРЅРёРµ РЅР°РїСЂСЏРјСѓСЋ С‡РµСЂРµР· Telegram API
            telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": telegram_id,
                "text": message_text,
                "parse_mode": "HTML"
            }
            
            response = requests.post(telegram_api_url, json=payload, timeout=5)
            if response.status_code == 200:
                pass  # print(f"вњ… РЈРІРµРґРѕРјР»РµРЅРёРµ РѕР± СѓРІРѕР»СЊРЅРµРЅРёРё РѕС‚РїСЂР°РІР»РµРЅРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЋ {telegram_id}")
            else:
                pass  # print(f"вљ пёЏ РћС€РёР±РєР° РїСЂРё РѕС‚РїСЂР°РІРєРµ СѓРІРµРґРѕРјР»РµРЅРёСЏ: {response.text}")
                
        except Exception as e:
            pass  # print(f"вљ пёЏ РћС€РёР±РєР° РїСЂРё РѕС‚РїСЂР°РІРєРµ СѓРІРµРґРѕРјР»РµРЅРёСЏ РѕР± СѓРІРѕР»СЊРЅРµРЅРёРё: {e}")
    
    return {
        "message": f"РџРµСЂСЃРѕРЅР°Р» '{staff_name}' СѓРґР°Р»РµРЅ",
        "deleted_id": staff_id,
        "status": staff.status
    }


@bp.route("/staff/<int:staff_id>/photo", methods=["POST"])
def upload_staff_photo(staff_id):
    """
    Р—Р°РіСЂСѓР¶Р°РµС‚ С„РѕС‚Рѕ СЃРѕС‚СЂСѓРґРЅРёРєР°
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "РЎРѕС‚СЂСѓРґРЅРёРє РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Р¤Р°Р№Р» РЅРµ РїСЂРµРґРѕСЃС‚Р°РІР»РµРЅ"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Р¤Р°Р№Р» РЅРµ РІС‹Р±СЂР°РЅ"}, 400
    
    # РџСЂРѕРІРµСЂСЏРµРј СЂР°СЃС€РёСЂРµРЅРёРµ
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Р”РѕРїСѓСЃС‚РёРјС‹Рµ С„РѕСЂРјР°С‚С‹: jpg, jpeg, png, gif"}, 400
    
    try:
        # РЈРґР°Р»СЏРµРј СЃС‚Р°СЂРѕРµ С„РѕС‚Рѕ РµСЃР»Рё СЃСѓС‰РµСЃС‚РІСѓРµС‚
        if staff.photo_path:
            delete_user_photo(staff.photo_path)
        
        # РЎРѕС…СЂР°РЅСЏРµРј РЅРѕРІРѕРµ С„РѕС‚Рѕ РІ РїР°РїРєСѓ teachers
        file_data = file.read()
        filename = "photo." + file.filename.rsplit('.', 1)[1].lower()
        
        from dance_studio.core.media_manager import TEACHERS_MEDIA_DIR
        staff_dir = os.path.join(TEACHERS_MEDIA_DIR, str(staff_id))
        os.makedirs(staff_dir, exist_ok=True)
        
        file_path = os.path.join(staff_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        photo_path = os.path.relpath(file_path, BASE_DIR)
        
        staff.photo_path = photo_path
        db.commit()
        
        return {
            "id": staff.id,
            "photo_path": staff.photo_path,
            "message": "Р¤РѕС‚Рѕ СѓСЃРїРµС€РЅРѕ Р·Р°РіСЂСѓР¶РµРЅРѕ"
        }, 201
    
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё Р·Р°РіСЂСѓР·РєРµ С„РѕС‚Рѕ: {e}")
        return {"error": str(e)}, 500


@bp.route("/staff/<int:staff_id>/photo", methods=["DELETE"])
def delete_staff_photo(staff_id):
    """
    РЈРґР°Р»СЏРµС‚ С„РѕС‚Рѕ СЃРѕС‚СЂСѓРґРЅРёРєР°
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "РЎРѕС‚СЂСѓРґРЅРёРє РЅРµ РЅР°Р№РґРµРЅ"}, 404
    
    if not staff.photo_path:
        return {"error": "Р¤РѕС‚Рѕ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    
    try:
        delete_user_photo(staff.photo_path)
        staff.photo_path = None
        db.commit()
        
        return {"ok": True, "message": "Р¤РѕС‚Рѕ СѓРґР°Р»РµРЅРѕ"}
    
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё СѓРґР°Р»РµРЅРёРё С„РѕС‚Рѕ: {e}")
        return {"error": str(e)}, 500


@bp.route("/api/teachers", methods=["GET"])
def list_public_teachers():
    db = g.db
    teachers = db.query(Staff).filter(
        Staff.status == "active",
        or_(
            Staff.teaches == 1,
            (Staff.position.in_(["СѓС‡РёС‚РµР»СЊ", "РЈС‡РёС‚РµР»СЊ"]) & Staff.teaches.is_(None))
        )
    ).all()

    return jsonify([
        {
            "id": t.id,
            "name": t.name,
            "position": t.position,
            "specialization": t.specialization,
            "bio": t.bio,
            "photo": t.photo_path,
        }
        for t in teachers
    ])


@bp.route("/api/teachers/<int:teacher_id>", methods=["GET"])
def get_public_teacher(teacher_id):
    db = g.db
    teacher = (
        db.query(Staff)
        .filter(
            Staff.id == teacher_id,
            Staff.status == "active",
            or_(
                Staff.teaches == 1,
                (Staff.position.in_(["СѓС‡РёС‚РµР»СЊ", "РЈС‡РёС‚РµР»СЊ"]) & Staff.teaches.is_(None))
            )
        )
        .first()
    )
    if not teacher:
        return {"error": "РџСЂРµРїРѕРґР°РІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404
    groups = (
        db.query(Group)
        .filter(Group.teacher_id == teacher.id)
        .order_by(Group.created_at.desc())
        .all()
    )
    group_items = []
    for group in groups:
        direction = db.query(Direction).filter(Direction.direction_id == group.direction_id).first()
        group_items.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "age_group": group.age_group,
            "duration_minutes": group.duration_minutes,
            "lessons_per_week": group.lessons_per_week,
            "max_students": group.max_students,
            "direction_id": direction.direction_id if direction else group.direction_id,
            "direction_title": direction.title if direction else None,
            "direction_type": direction.direction_type if direction else None,
            "direction_status": direction.status if direction else None,
            "direction_image": _build_image_url(direction.image_path) if direction else None,
        })
    return {
        "id": teacher.id,
        "name": teacher.name,
        "position": teacher.position,
        "specialization": teacher.specialization,
        "bio": teacher.bio,
        "photo": teacher.photo_path,
        "groups": group_items,
    }


@bp.route("/api/teachers/<int:teacher_id>/schedule", methods=["GET"])
def get_public_teacher_schedule(teacher_id):
    db = g.db
    teacher_exists = db.query(Staff).filter(
        Staff.id == teacher_id,
        Staff.status == "active",
        or_(
            Staff.teaches == 1,
            (Staff.position.in_(["СѓС‡РёС‚РµР»СЊ", "РЈС‡РёС‚РµР»СЊ"]) & Staff.teaches.is_(None))
        )
    ).first()
    if not teacher_exists:
        return {"error": "РџСЂРµРїРѕРґР°РІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404
    items = (
        db.query(TeacherWorkingHours)
        .filter_by(teacher_id=teacher_id, status="active")
        .order_by(TeacherWorkingHours.weekday.asc(), TeacherWorkingHours.time_from.asc())
        .all()
    )
    return [
        {
            "weekday": i.weekday,
            "time_from": i.time_from.strftime("%H:%M") if i.time_from else None,
            "time_to": i.time_to.strftime("%H:%M") if i.time_to else None,
            "valid_from": i.valid_from.isoformat() if i.valid_from else None,
            "valid_to": i.valid_to.isoformat() if i.valid_to else None,
        }
        for i in items
    ]


@bp.route("/api/teachers/<int:teacher_id>/availability", methods=["GET"])
def get_teacher_availability(teacher_id):
    db = g.db
    teacher = db.query(Staff).filter(Staff.id == teacher_id, Staff.status == "active").first()
    if not teacher:
        return {"error": "РџСЂРµРїРѕРґР°РІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    start_str = request.args.get("start")
    days_str = request.args.get("days")
    duration_str = request.args.get("duration")
    step_str = request.args.get("step")

    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else date.today()
    except ValueError:
        return {"error": "start РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ YYYY-MM-DD"}, 400

    def _parse_positive_int(value, default, min_value, max_value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < min_value:
            return min_value
        if max_value is not None and parsed > max_value:
            return max_value
        return parsed

    days_count = _parse_positive_int(days_str, 7, 1, 21)
    duration_minutes = _parse_positive_int(duration_str, 60, 15, 240)
    step_minutes = _parse_positive_int(step_str, 30, 15, 180)

    working_hours = (
        db.query(TeacherWorkingHours)
        .filter_by(teacher_id=teacher_id, status="active")
        .all()
    )

    dates = []
    for offset in range(days_count):
        day = start_date + timedelta(days=offset)
        weekday = day.weekday()
        entries = [
            entry
            for entry in working_hours
            if entry.weekday == weekday
            and (not entry.valid_from or entry.valid_from <= day)
            and (not entry.valid_to or entry.valid_to >= day)
            and entry.time_from
            and entry.time_to
            and entry.time_to > entry.time_from
        ]
        busy_intervals = _collect_busy_intervals(db, teacher_id, day)
        busy_intervals.sort()
        slots = []
        seen = set()
        free_ranges = []
        for entry in entries:
            start_min = _time_to_minutes(entry.time_from)
            end_min = _time_to_minutes(entry.time_to)
            last_start = end_min - duration_minutes
            current = start_min
            while current <= last_start:
                if not _has_slot_conflict(current, duration_minutes, busy_intervals):
                    slot_str = _minutes_to_time_str(current)
                    if slot_str not in seen:
                        seen.add(slot_str)
                        slots.append(slot_str)
                current += step_minutes
            segments = _subtract_busy_intervals(start_min, end_min, busy_intervals)
            for seg_start, seg_end in segments:
                if seg_end - seg_start >= step_minutes:
                    free_ranges.append({
                        "from": _minutes_to_time_str(seg_start),
                        "to": _minutes_to_time_str(seg_end),
                        "from_minutes": seg_start,
                        "to_minutes": seg_end,
                    })
        dates.append(
            {
                "date": day.isoformat(),
                "weekday": weekday,
                "slots": slots,
                "free_ranges": free_ranges,
            }
        )

    return {
        "teacher_id": teacher_id,
        "teacher_name": teacher.name,
        "duration_minutes": duration_minutes,
        "slot_step_minutes": step_minutes,
        "dates": dates,
    }


@bp.route("/staff/list/all")
def list_all_staff():
    """
    Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃРїРёСЃРѕРє РІСЃРµРіРѕ РїРµСЂСЃРѕРЅР°Р»Р° РґР»СЏ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂРѕРІ
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter(Staff.status != "dismissed").all()
    
    result = []
    for s in staff:
        # РџРѕР»СѓС‡Р°РµРј username РёР· User РµСЃР»Рё РµСЃС‚СЊ telegram_id
        username = None
        if s.telegram_id:
            user = db.query(User).filter_by(telegram_id=s.telegram_id).first()
            if user:
                username = user.username
        
        result.append({
            "id": s.id,
            "name": s.name,
            "position": s.position,
            "specialization": s.specialization,
            "phone": s.phone,
            "email": s.email,
            "telegram_id": s.telegram_id,
            "username": username,
            "photo": s.photo_path,
            "teaches": s.teaches,
            "status": s.status,
            "bio": s.bio
        })
    
    return jsonify(result)


@bp.route("/staff/search")
def search_staff():
    """
    РџРѕРёСЃРє РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ РґР»СЏ РґРѕР±Р°РІР»РµРЅРёСЏ РІ РїРµСЂСЃРѕРЅР°Р».
    РџР°СЂР°РјРµС‚СЂС‹ query:
    - q: СЃС‚СЂРѕРєР° РїРѕРёСЃРєР° (РµСЃР»Рё РЅРµ СѓРєР°Р·Р°РЅР°, РІРѕР·РІСЂР°С‰Р°РµС‚ РІСЃРµС… РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№)
    - by_username: РµСЃР»Рё True, РёС‰РµС‚ С‚РѕР»СЊРєРѕ РїРѕ СЋР·РµСЂРЅРµР№РјСѓ (РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РїСЂРё @username)
    """
    try:
        db = g.db
        search_query = request.args.get('q', '').strip().lower()
        by_username = request.args.get('by_username', 'false').lower() == 'true'
        
        # С‰РµРј СЃСЂРµРґРё РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ (Users), Р° РЅРµ СЃСЂРµРґРё РїРµСЂСЃРѕРЅР°Р»Р° (Staff)
        users = db.query(User).all()
        result = []
        
        # Р•СЃР»Рё РЅРµС‚ РїРѕРёСЃРєРѕРІРѕРіРѕ Р·Р°РїСЂРѕСЃР°, РІРѕР·РІСЂР°С‰Р°РµРј РІСЃРµС… РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№
        if not search_query:
            result = [
                {
                    "id": u.id,
                    "name": u.name,
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "phone": u.phone,
                    "email": u.email
                }
                for u in users
            ]
        else:
            # Р’С‹РїРѕР»РЅСЏРµРј С„РёР»СЊС‚СЂ РІ Р·Р°РІРёСЃРёРјРѕСЃС‚Рё РѕС‚ С‚РёРїР° РїРѕРёСЃРєР°
            for u in users:
                if by_username:
                    # РџРѕРёСЃРє С‚РѕР»СЊРєРѕ РїРѕ СЋР·РµСЂРЅРµР№РјСѓ (РїСЂРё РІРІРѕРґРµ @username)
                    if u.username:
                        # РќРѕСЂРјР°Р»РёР·СѓРµРј: СѓР±РёСЂР°РµРј @ РёР· РѕР±РѕРёС… СЃС‚СЂРѕРє РґР»СЏ СЃСЂР°РІРЅРµРЅРёСЏ
                        username_clean = u.username.lower().replace('@', '')
                        search_clean = search_query.replace('@', '')
                        if search_clean in username_clean or username_clean.startswith(search_clean):
                            result.append({
                                "id": u.id,
                                "name": u.name,
                                "telegram_id": u.telegram_id,
                                "username": u.username,
                                "phone": u.phone,
                                "email": u.email
                            })
                else:
                    # РџРѕРёСЃРє РїРѕ РёРјРµРЅРё РёР»Рё telegram_id (РїСЂРё РѕР±С‹С‡РЅРѕРј РІРІРѕРґРµ)
                    if (u.name.lower().startswith(search_query) or 
                        (u.telegram_id and str(u.telegram_id).startswith(search_query))):
                        result.append({
                            "id": u.id,
                            "name": u.name,
                            "telegram_id": u.telegram_id,
                            "username": u.username,
                            "phone": u.phone,
                            "email": u.email
                        })
        
        return jsonify(result)
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё РїРѕРёСЃРєРµ РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/search-users")
def search_users():
    """РџРѕРёСЃРє РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ РґР»СЏ СЂР°СЃСЃС‹Р»РѕРє"""
    db = g.db
    try:
        search_query = request.args.get('query', '').strip().lower()
        
        if not search_query:
            return jsonify([]), 200
        
        users = db.query(User).all()
        result = []
        
        for u in users:
            # РџРѕРёСЃРє РїРѕ РёРјРµРЅРё РёР»Рё telegram_id
            if (u.name.lower().find(search_query) != -1 or 
                (u.telegram_id and str(u.telegram_id).startswith(search_query))):
                result.append({
                    "id": u.id,
                    "name": u.name,
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "phone": u.phone,
                    "email": u.email
                })
        
        return jsonify(result)
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё РїРѕРёСЃРєРµ РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/mailings", methods=["GET"])
def get_mailings():
    """РџРѕР»СѓС‡Р°РµС‚ РІСЃРµ СЂР°СЃСЃС‹Р»РєРё (РґР»СЏ СѓРїСЂР°РІР»РµРЅРёСЏ)"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        mailings = db.query(Mailing).order_by(Mailing.created_at.desc()).all()
        
        result = []
        for m in mailings:
            result.append({
                "mailing_id": m.mailing_id,
                "creator_id": m.creator_id,
                "name": m.name,
                "description": m.description,
                "purpose": m.purpose,
                "status": m.status,
                "target_type": m.target_type,
                "target_id": m.target_id,
                "mailing_type": m.mailing_type,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
                "created_at": m.created_at.isoformat()
            })
        
        return jsonify(result)
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё РїРѕР»СѓС‡РµРЅРёРё СЂР°СЃСЃС‹Р»РѕРє: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/mailings", methods=["POST"])
def create_mailing():
    """РЎРѕР·РґР°РµС‚ РЅРѕРІСѓСЋ СЂР°СЃСЃС‹Р»РєСѓ"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    try:
        # РћР±СЏР·Р°С‚РµР»СЊРЅС‹Рµ РїРѕР»СЏ
        if not data.get("creator_id") or not data.get("name") or not data.get("purpose") or not data.get("target_type"):
            return {"error": "creator_id, name, purpose Рё target_type РѕР±СЏР·Р°С‚РµР»СЊРЅС‹"}, 400
        
        # РћРїСЂРµРґРµР»СЏРµРј СЃС‚Р°С‚СѓСЃ РЅР° РѕСЃРЅРѕРІРµ РІС‹Р±РѕСЂР° РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ
        send_now = data.get("send_now", False)
        
        # Р•СЃР»Рё РѕС‚РїСЂР°РІР»СЏРµРј СЃРµР№С‡Р°СЃ, СЃС‚Р°С‚СѓСЃ = "pending" (Р¶РґРµС‚ РѕС‚РїСЂР°РІРєРё)
        # Р•СЃР»Рё РѕС‚РїСЂР°РІР»СЏРµРј РїРѕР·Р¶Рµ, СЃС‚Р°С‚СѓСЃ = "scheduled"
        status = "pending" if send_now else "scheduled"
        
        # Р•СЃР»Рё РЅСѓР¶РЅРѕ РѕС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ
        sent_at = None
        if send_now:
            sent_at = None  # РћС‚РїСЂР°РІР»СЏРµС‚СЃСЏ РІ РїСЂРѕС†РµСЃСЃРµ, sent_at СѓСЃС‚Р°РЅРѕРІРёС‚СЃСЏ РїРѕСЃР»Рµ РѕС‚РїСЂР°РІРєРё
        
        scheduled_at = data.get("scheduled_at")
        
        # Р•СЃР»Рё СЌС‚Рѕ РѕС‚Р»РѕР¶РµРЅРЅР°СЏ СЂР°СЃСЃС‹Р»РєР°, РЅСѓР¶РЅРѕ РІСЂРµРјСЏ
        if not send_now and not scheduled_at:
            return {"error": "Р”Р»СЏ РѕС‚Р»РѕР¶РµРЅРЅРѕР№ СЂР°СЃСЃС‹Р»РєРё С‚СЂРµР±СѓРµС‚СЃСЏ scheduled_at"}, 400
        
        # Р•СЃР»Рё scheduled_at РїРµСЂРµРґР°РЅР° РєР°Рє СЃС‚СЂРѕРєР°, РєРѕРЅРІРµСЂС‚РёСЂСѓРµРј РІ datetime
        if scheduled_at and isinstance(scheduled_at, str):
            # РЈР±РµР¶РґР°РµРјСЃСЏ С‡С‚Рѕ РµСЃС‚СЊ СЃРµРєСѓРЅРґС‹ РІ СЃС‚СЂРѕРєРµ (datetime-local РјРѕР¶РµС‚ РёС… РЅРµ СЃРѕРґРµСЂР¶Р°С‚СЊ)
            if 'T' in scheduled_at and scheduled_at.count(':') == 1:
                scheduled_at = scheduled_at + ':00'  # Р”РѕР±Р°РІР»СЏРµРј :00 РґР»СЏ СЃРµРєСѓРЅРґ
            try:
                scheduled_at = datetime.fromisoformat(scheduled_at)
            except ValueError as e:
                return {"error": f"РќРµРІРµСЂРЅС‹Р№ С„РѕСЂРјР°С‚ РґР°С‚С‹: {e}"}, 400
        
        mailing = Mailing(
            creator_id=data["creator_id"],
            name=data["name"],
            description=data.get("description"),
            purpose=data["purpose"],
            status=status,
            target_type=data["target_type"],
            target_id=data.get("target_id"),
            mailing_type=data.get("mailing_type", "manual"),  # РџРѕ СѓРјРѕР»С‡Р°РЅРёСЋ - СЂСѓС‡РЅР°СЏ СЂР°СЃСЃС‹Р»РєР°
            sent_at=sent_at,
            scheduled_at=scheduled_at
        )
        
        db.add(mailing)
        db.commit()
        
        # Р•СЃР»Рё РЅСѓР¶РЅРѕ РѕС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ, РґРѕР±Р°РІР»СЏРµРј РІ РѕС‡РµСЂРµРґСЊ РѕС‚РїСЂР°РІРєРё
        if send_now:
            from dance_studio.bot.bot import queue_mailing_for_sending
            queue_mailing_for_sending(mailing.mailing_id)
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }, 201
    
    except Exception as e:
        db.rollback()
        print(f"РћС€РёР±РєР° РїСЂРё СЃРѕР·РґР°РЅРёРё СЂР°СЃСЃС‹Р»РєРё: {e}")
        return {"error": str(e)}, 500


@bp.route("/mailings/<int:mailing_id>", methods=["GET"])
def get_mailing(mailing_id):
    """РџРѕР»СѓС‡Р°РµС‚ РёРЅС„РѕСЂРјР°С†РёСЋ Рѕ РєРѕРЅРєСЂРµС‚РЅРѕР№ СЂР°СЃСЃС‹Р»РєРµ"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Р Р°СЃСЃС‹Р»РєР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }
    
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё РїРѕР»СѓС‡РµРЅРёРё СЂР°СЃСЃС‹Р»РєРё: {e}")
        return {"error": str(e)}, 500


@bp.route("/mailings/<int:mailing_id>", methods=["PUT"])
def update_mailing(mailing_id):
    """РћР±РЅРѕРІР»СЏРµС‚ СЂР°СЃСЃС‹Р»РєСѓ"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Р Р°СЃСЃС‹Р»РєР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404
        
        # РћР±РЅРѕРІР»СЏРµРј РїРѕР»СЏ
        if "name" in data:
            mailing.name = data["name"]
        if "description" in data:
            mailing.description = data["description"]
        if "purpose" in data:
            mailing.purpose = data["purpose"]
        if "status" in data:
            mailing.status = data["status"]
        if "target_type" in data:
            mailing.target_type = data["target_type"]
        if "target_id" in data:
            mailing.target_id = data["target_id"]
        if "mailing_type" in data:
            mailing.mailing_type = data["mailing_type"]
        if "sent_at" in data:
            mailing.sent_at = datetime.fromisoformat(data["sent_at"]) if data["sent_at"] else None
        if "scheduled_at" in data:
            mailing.scheduled_at = datetime.fromisoformat(data["scheduled_at"]) if data["scheduled_at"] else None
        
        db.commit()
        
        return {
            "mailing_id": mailing.mailing_id,
            "creator_id": mailing.creator_id,
            "name": mailing.name,
            "description": mailing.description,
            "purpose": mailing.purpose,
            "status": mailing.status,
            "target_type": mailing.target_type,
            "target_id": mailing.target_id,
            "mailing_type": mailing.mailing_type,
            "sent_at": mailing.sent_at.isoformat() if mailing.sent_at else None,
            "scheduled_at": mailing.scheduled_at.isoformat() if mailing.scheduled_at else None,
            "created_at": mailing.created_at.isoformat()
        }
    
    except Exception as e:
        db.rollback()
        print(f"РћС€РёР±РєР° РїСЂРё РѕР±РЅРѕРІР»РµРЅРёРё СЂР°СЃСЃС‹Р»РєРё: {e}")
        return {"error": str(e)}, 500


@bp.route("/mailings/<int:mailing_id>", methods=["DELETE"])
def delete_mailing(mailing_id):
    """РЈРґР°Р»СЏРµС‚ СЂР°СЃСЃС‹Р»РєСѓ (РёР»Рё РѕС‚РјРµРЅСЏРµС‚ РµС‘)"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Р Р°СЃСЃС‹Р»РєР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404
        
        # РЈСЃС‚Р°РЅР°РІР»РёРІР°РµРј СЃС‚Р°С‚СѓСЃ "РѕС‚РјРµРЅРµРЅРѕ" РІРјРµСЃС‚Рѕ СѓРґР°Р»РµРЅРёСЏ
        mailing.status = "cancelled"
        db.commit()
        
        return {"message": "Р Р°СЃСЃС‹Р»РєР° РѕС‚РјРµРЅРµРЅР°"}, 200
    
    except Exception as e:
        db.rollback()
        print(f"РћС€РёР±РєР° РїСЂРё СѓРґР°Р»РµРЅРёРё СЂР°СЃСЃС‹Р»РєРё: {e}")
        return {"error": str(e)}, 500


@bp.route("/mailings/<int:mailing_id>/send", methods=["POST"])
def send_mailing_endpoint(mailing_id):
    """РЅРёС†РёРёСЂСѓРµС‚ РѕС‚РїСЂР°РІРєСѓ СЂР°СЃСЃС‹Р»РєРё"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    try:
        # РјРїРѕСЂС‚РёСЂСѓРµРј С„СѓРЅРєС†РёСЋ РґРѕР±Р°РІР»РµРЅРёСЏ СЂР°СЃСЃС‹Р»РєРё РІ РѕС‡РµСЂРµРґСЊ
        from dance_studio.bot.bot import queue_mailing_for_sending
        
        db = g.db
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Р Р°СЃСЃС‹Р»РєР° РЅРµ РЅР°Р№РґРµРЅР°"}, 404
        
        # РџСЂРѕРІРµСЂСЏРµРј, РЅРµ РѕС‚РїСЂР°РІР»РµРЅР° Р»Рё СѓР¶Рµ
        if mailing.status == "sent":
            return {"error": "Р Р°СЃСЃС‹Р»РєР° СѓР¶Рµ Р±С‹Р»Р° РѕС‚РїСЂР°РІР»РµРЅР°"}, 400
        
        if mailing.status == "cancelled":
            return {"error": "Р Р°СЃСЃС‹Р»РєР° Р±С‹Р»Р° РѕС‚РјРµРЅРµРЅР°"}, 400
        
        # Р”РѕР±Р°РІР»СЏРµРј СЂР°СЃСЃС‹Р»РєСѓ РІ РѕС‡РµСЂРµРґСЊ РЅР° РѕС‚РїСЂР°РІРєСѓ
        queue_mailing_for_sending(mailing_id)
        
        return {"message": f"Р Р°СЃСЃС‹Р»РєР° '{mailing.name}' РґРѕР±Р°РІР»РµРЅР° РІ РѕС‡РµСЂРµРґСЊ РѕС‚РїСЂР°РІРєРё", "status": "pending"}, 200
    
    except Exception as e:
        print(f"РћС€РёР±РєР° РїСЂРё РѕС‚РїСЂР°РІРєРµ СЂР°СЃСЃС‹Р»РєРё: {e}")
        return {"error": str(e)}, 500


@bp.route("/api/directions", methods=["GET"])
def get_directions():
    """РџРѕР»СѓС‡Р°РµС‚ РІСЃРµ Р°РєС‚РёРІРЅС‹Рµ РЅР°РїСЂР°РІР»РµРЅРёСЏ"""
    db = g.db
    direction_type = request.args.get("direction_type") or request.args.get("type")
    query = db.query(Direction).filter_by(status="active")
    if direction_type:
        direction_type = direction_type.lower()
        if direction_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ 'dance' РёР»Рё 'sport'"}, 400
        query = query.filter(Direction.direction_type == direction_type)

    directions = query.order_by(Direction.created_at.desc()).all()

    #print(f"вњ“ РќР°Р№РґРµРЅРѕ {len(directions)} Р°РєС‚РёРІРЅС‹С… РЅР°РїСЂР°РІР»РµРЅРёР№")
    
    result = []
    for d in directions:
        image_url = _build_image_url(d.image_path)
        groups_count = db.query(Group).filter_by(direction_id=d.direction_id).count()
        
        result.append({
            "direction_id": d.direction_id,
            "direction_type": d.direction_type or "dance",
            "title": d.title,
            "description": d.description,
            "base_price": d.base_price,
            "is_popular": d.is_popular,
            "image_path": image_url,
            "created_at": d.created_at.isoformat(),
            "groups_count": groups_count
        })
    
    return jsonify(result)


@bp.route("/api/directions/manage", methods=["GET"])
def get_directions_manage():
    """РџРѕР»СѓС‡Р°РµС‚ РІСЃРµ РЅР°РїСЂР°РІР»РµРЅРёСЏ РґР»СЏ СѓРїСЂР°РІР»РµРЅРёСЏ (РІРєР»СЋС‡Р°СЏ РЅРµР°РєС‚РёРІРЅС‹Рµ)"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    direction_type = request.args.get("direction_type") or request.args.get("type")
    query = db.query(Direction)
    if direction_type:
        direction_type = direction_type.lower()
        if direction_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ 'dance' РёР»Рё 'sport'"}, 400
        query = query.filter(Direction.direction_type == direction_type)

    directions = query.order_by(Direction.created_at.desc()).all()
    
    result = []
    for d in directions:
        image_url = _build_image_url(d.image_path)
        groups_count = db.query(Group).filter_by(direction_id=d.direction_id).count()
        
        result.append({
            "direction_id": d.direction_id,
            "direction_type": d.direction_type or "dance",
            "title": d.title,
            "description": d.description,
            "base_price": d.base_price,
            "is_popular": d.is_popular,
            "status": d.status,
            "image_path": image_url,
            "created_at": d.created_at.isoformat(),
            "updated_at": d.updated_at.isoformat(),
            "groups_count": groups_count
        })
    
    return jsonify(result)


@bp.route("/api/directions/<int:direction_id>", methods=["GET"])
def get_direction(direction_id):
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ РѕРґРЅРѕ РЅР°РїСЂР°РІР»РµРЅРёРµ РїРѕ ID РґР»СЏ С„РѕСЂРјС‹ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "РќР°РїСЂР°РІР»РµРЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    image_url = _build_image_url(direction.image_path)

    return jsonify({
        "direction_id": direction.direction_id,
        "direction_type": direction.direction_type or "dance",
        "title": direction.title,
        "description": direction.description,
        "base_price": direction.base_price,
        "is_popular": direction.is_popular,
        "status": direction.status,
        "image_path": image_url,
        "created_at": direction.created_at.isoformat(),
        "updated_at": direction.updated_at.isoformat()
    })


@bp.route("/api/directions/<int:direction_id>/groups", methods=["GET"])
def get_direction_groups(direction_id):
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃРїРёСЃРѕРє РіСЂСѓРїРї РґР»СЏ РЅР°РїСЂР°РІР»РµРЅРёСЏ"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "РќР°РїСЂР°РІР»РµРЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    groups = db.query(Group).filter_by(direction_id=direction_id).order_by(Group.created_at.desc()).all()
    result = []
    for gr in groups:
        teacher_name = gr.teacher.name if gr.teacher else None
        teacher_photo = None
        if gr.teacher and gr.teacher.photo_path:
            teacher_photo = "/" + gr.teacher.photo_path.replace("\\", "/")
        result.append({
            "id": gr.id,
            "direction_id": gr.direction_id,
            "direction_type": direction.direction_type,
            "direction_title": direction.title,
            "teacher_id": gr.teacher_id,
            "teacher_name": teacher_name,
            "teacher_photo": teacher_photo,
            "name": gr.name,
            "description": gr.description,
            "age_group": gr.age_group,
            "max_students": gr.max_students,
            "duration_minutes": gr.duration_minutes,
            "lessons_per_week": gr.lessons_per_week,
            "created_at": gr.created_at.isoformat()
        })

    return jsonify(result)


@bp.route("/api/directions/<int:direction_id>/groups", methods=["POST"])
def create_direction_group(direction_id):
    """РЎРѕР·РґР°РµС‚ РіСЂСѓРїРїСѓ РІРЅСѓС‚СЂРё РЅР°РїСЂР°РІР»РµРЅРёСЏ"""
    perm_error = require_permission("create_group")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}

    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "РќР°РїСЂР°РІР»РµРЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404

    name = data.get("name")
    teacher_id = data.get("teacher_id")
    age_group = data.get("age_group")
    max_students = data.get("max_students")
    duration_minutes = data.get("duration_minutes")
    lessons_per_week = data.get("lessons_per_week")
    description = data.get("description")

    if not name or not teacher_id or not age_group or not max_students or not duration_minutes:
        return {"error": "name, teacher_id, age_group, max_students, duration_minutes РѕР±СЏР·Р°С‚РµР»СЊРЅС‹"}, 400

    teacher = db.query(Staff).filter_by(id=teacher_id).first()
    if not teacher:
        return {"error": "РџСЂРµРїРѕРґР°РІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    try:
        max_students_int = int(max_students)
        duration_minutes_int = int(duration_minutes)
    except ValueError:
        return {"error": "max_students Рё duration_minutes РґРѕР»Р¶РЅС‹ Р±С‹С‚СЊ С‡РёСЃР»Р°РјРё"}, 400

    lessons_per_week_int = None
    if lessons_per_week is not None and lessons_per_week != "":
        try:
            lessons_per_week_int = int(lessons_per_week)
        except ValueError:
            return {"error": "lessons_per_week РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј"}, 400

    group = Group(
        direction_id=direction_id,
        teacher_id=teacher_id,
        name=name,
        description=description,
        age_group=age_group,
        max_students=max_students_int,
        duration_minutes=duration_minutes_int,
        lessons_per_week=lessons_per_week_int
    )
    db.add(group)
    db.commit()

    # РЎРѕР·РґР°РµРј С‡Р°С‚ Telegram С‡РµСЂРµР· userbot Рё РґРѕР±Р°РІР»СЏРµРј РїСЂРµРїРѕРґР°РІР°С‚РµР»СЏ
    if teacher.telegram_id:
        try:
            from dance_studio.bot.telegram_userbot import create_group_chat_sync

            teacher_user = db.query(User).filter_by(telegram_id=teacher.telegram_id).first()
            chat_info = create_group_chat_sync(
                name,
                [{
                    "id": teacher.telegram_id,
                    "username": getattr(teacher_user, "username", None),
                    "phone": teacher.phone,
                    "name": teacher.name,
                }],
            )
        except Exception as e:
            print(f"[create_direction_group] Telegram chat creation failed: {e}")
            chat_info = None
        if chat_info:
            group.chat_id = chat_info.get("chat_id")
            group.chat_invite_link = chat_info.get("invite_link")
            failed = chat_info.get("failed_user_ids") or []

            # Р’СЃРµРіРґР° С€Р»С‘Рј СЃСЃС‹Р»РєСѓ РїСЂРµРїРѕРґР°РІР°С‚РµР»СЋ, РґР°Р¶Рµ РµСЃР»Рё invite СЃСЂР°Р±РѕС‚Р°Р» вЂ” РЅР° СЃР»СѓС‡Р°Р№ РїСЂРёРІР°С‚РЅРѕСЃС‚Рё.
            target_ids = {teacher.telegram_id} | {uid for uid in failed if uid}
            for uid in target_ids:
                try:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": int(uid),
                            "text": f"РџСЂРёСЃРѕРµРґРёРЅРёС‚СЊСЃСЏ Рє С‡Р°С‚Сѓ РіСЂСѓРїРїС‹ \"{name}\" РјРѕР¶РЅРѕ РїРѕ СЃСЃС‹Р»РєРµ: {group.chat_invite_link}",
                            "disable_web_page_preview": True,
                        },
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        print(f"[create_direction_group] sendMessage to {uid} failed: {resp.status_code} {resp.text}")
                except Exception as send_err:
                    print(f"[create_direction_group] РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ СЃСЃС‹Р»РєСѓ РїРѕР»СЊР·РѕРІР°С‚РµР»СЋ {uid}: {send_err}")
            db.commit()

    return {
        "id": group.id,
        "direction_id": group.direction_id,
        "teacher_id": group.teacher_id,
        "teacher_name": teacher.name,
        "name": group.name,
        "description": group.description,
        "age_group": group.age_group,
        "max_students": group.max_students,
        "duration_minutes": group.duration_minutes,
        "lessons_per_week": group.lessons_per_week,
        "chat_id": group.chat_id,
        "chat_invite_link": group.chat_invite_link,
        "created_at": group.created_at.isoformat()
    }, 201


@bp.route("/api/directions/create-session", methods=["POST"])
def create_direction_upload_session():
    """
    РЎРѕР·РґР°РµС‚ СЃРµСЃСЃРёСЋ Р·Р°РіСЂСѓР·РєРё РЅР°РїСЂР°РІР»РµРЅРёСЏ.
    РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ Р·Р°РїРѕР»РЅСЏРµС‚ С„РѕСЂРјСѓ Рё РїРѕР»СѓС‡Р°РµС‚ С‚РѕРєРµРЅ РґР»СЏ Р±РѕС‚Р°.
    """
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}
    
    telegram_user_id = getattr(g, "telegram_id", None)
    if not telegram_user_id:
        return {"error": "РўСЂРµР±СѓРµС‚СЃСЏ Р°РІС‚РѕСЂРёР·Р°С†РёСЏ"}, 401

    try:
        telegram_user_id = int(telegram_user_id)
    except (TypeError, ValueError):
        return {"error": "РќРµРІРµСЂРЅС‹Р№ telegram_id"}, 400

    admin = db.query(Staff).filter_by(telegram_id=telegram_user_id).first()
    if not admin or admin.position not in ["Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ", "СЃС‚Р°СЂС€РёР№ Р°РґРјРёРЅ", "РІР»Р°РґРµР»РµС†", "С‚РµС…. Р°РґРјРёРЅ"]:
        return {"error": "РЈ РІР°СЃ РЅРµС‚ РїСЂР°РІ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°"}, 403
    
    # РћР±СЏР·Р°С‚РµР»СЊРЅС‹Рµ РїРѕР»СЏ
    required_fields = ["title", "description", "base_price"]
    for field in required_fields:
        if not data.get(field):
            return {"error": f"{field} РѕР±СЏР·Р°С‚РµР»РµРЅ"}, 400

    direction_type = (data.get("direction_type") or "dance").lower()
    if direction_type not in ALLOWED_DIRECTION_TYPES:
        return {"error": "direction_type РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ 'dance' РёР»Рё 'sport'"}, 400
    
    # РЎРѕР·РґР°РµРј СЃРµСЃСЃРёСЋ
    session_token = str(uuid.uuid4())
    
    session = DirectionUploadSession(
        admin_id=admin.id,
        telegram_user_id=telegram_user_id,
        title=data["title"],
        direction_type=direction_type,
        description=data["description"],
        base_price=data["base_price"],
        session_token=session_token,
        status="waiting_for_photo"
    )
    
    db.add(session)
    db.commit()
    
    return {
        "session_id": session.session_id,
        "session_token": session_token,
        "direction_type": direction_type,
        "message": "РЎРµСЃСЃРёСЏ СЃРѕР·РґР°РЅР°. РћС‚РїСЂР°РІСЊС‚Рµ С‚РѕРєРµРЅ Р±РѕС‚Сѓ РґР»СЏ Р·Р°РіСЂСѓР·РєРё С„РѕС‚РѕРіСЂР°С„РёРё."
    }, 201


@bp.route("/api/directions/upload-complete/<token>", methods=["GET"])
def get_upload_session_status(token):
    """РџСЂРѕРІРµСЂСЏРµС‚ СЃС‚Р°С‚СѓСЃ Р·Р°РіСЂСѓР·РєРё С„РѕС‚РѕРіСЂР°С„РёРё РїРѕ С‚РѕРєРµРЅСѓ"""
    try:
        db = g.db

        session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
        if not session:
            current_app.logger.warning("direction upload status: session not found token=%s", token)
            return {"error": "РЎРµСЃСЃРёСЏ РЅРµ РЅР°Р№РґРµРЅР°"}, 404

        current_app.logger.info(
            "direction upload status token=%s status=%s image=%s",
            token[:8],
            session.status,
            session.image_path,
        )

        return {
            "session_id": session.session_id,
            "status": session.status,
            "direction_type": session.direction_type or "dance",
            "image_path": _build_image_url(session.image_path),
            "title": session.title,
            "description": session.description,
            "base_price": session.base_price
        }
    except Exception as exc:
        import traceback, json
        trace = traceback.format_exc()
        current_app.logger.error("upload-complete error: %s\n%s", exc, trace)
        return {"error": "internal", "exception": str(exc), "trace": trace}, 500


@bp.route("/api/directions", methods=["POST"])
def create_direction():
    """РЎРѕР·РґР°РµС‚ РЅР°РїСЂР°РІР»РµРЅРёРµ РїРѕСЃР»Рµ Р·Р°РіСЂСѓР·РєРё С„РѕС‚Рѕ Р±РѕС‚РѕРј"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json

    print(f"[create_direction] request: {data}")

    session_token = data.get("session_token")
    if not session_token:
        return {"error": "session_token РѕР±СЏР·Р°С‚РµР»РµРЅ"}, 400

    session = db.query(DirectionUploadSession).filter_by(session_token=session_token).first()
    if not session:
        print(f"[create_direction] session not found: {session_token}")
        return {"error": "РЎРµСЃСЃРёСЏ РЅРµ РЅР°Р№РґРµРЅР°"}, 404

    print(f"[create_direction] session found: status={session.status}, photo={session.image_path}")

    if session.status != "photo_received":
        return {"error": f"РЎРµСЃСЃРёСЏ РЅРµ РіРѕС‚РѕРІР°. РЎС‚Р°С‚СѓСЃ: {session.status}"}, 400

    direction_type = (data.get("direction_type") or session.direction_type or "dance").lower()
    if direction_type not in ALLOWED_DIRECTION_TYPES:
        return {"error": "direction_type РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ 'dance' РёР»Рё 'sport'"}, 400

    direction = Direction(
        title=session.title,
        direction_type=direction_type,
        description=session.description,
        base_price=session.base_price,
        image_path=session.image_path,
        is_popular=data.get("is_popular", 0),
        status="active"
    )

    db.add(direction)
    db.commit()

    session.status = "completed"
    db.commit()

    print(f"[create_direction] created id={direction.direction_id}, title={direction.title}, type={direction.direction_type}")

    return {
        "direction_id": direction.direction_id,
        "title": direction.title,
        "direction_type": direction.direction_type,
        "message": "РќР°РїСЂР°РІР»РµРЅРёРµ СѓСЃРїРµС€РЅРѕ СЃРѕР·РґР°РЅРѕ"
    }, 201


@bp.route("/api/directions/<int:direction_id>", methods=["PUT"])
def update_direction(direction_id):
    """РћР±РЅРѕРІР»СЏРµС‚ РёРЅС„РѕСЂРјР°С†РёСЋ Рѕ РЅР°РїСЂР°РІР»РµРЅРёРё"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "РќР°РїСЂР°РІР»РµРЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    
    # РћР±РЅРѕРІР»СЏРµРј РїРѕР»СЏ
    if "title" in data:
        direction.title = data["title"]
    if "description" in data:
        direction.description = data["description"]
    if "base_price" in data:
        direction.base_price = data["base_price"]
    if "direction_type" in data:
        new_type = (data.get("direction_type") or "").lower()
        if new_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ 'dance' РёР»Рё 'sport'"}, 400
        direction.direction_type = new_type
    if "status" in data:
        direction.status = data["status"]
    if "is_popular" in data:
        direction.is_popular = data["is_popular"]
    
    db.commit()
    
    return {
        "direction_id": direction.direction_id,
        "direction_type": direction.direction_type,
        "message": "РќР°РїСЂР°РІР»РµРЅРёРµ РѕР±РЅРѕРІР»РµРЅРѕ"
    }


@bp.route("/api/directions/<int:direction_id>", methods=["DELETE"])
def delete_direction(direction_id):
    """РЈРґР°Р»СЏРµС‚ РЅР°РїСЂР°РІР»РµРЅРёРµ"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "РќР°РїСЂР°РІР»РµРЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ"}, 404
    
    direction.status = "inactive"
    db.commit()
    
    return {"message": "РќР°РїСЂР°РІР»РµРЅРёРµ СѓРґР°Р»РµРЅРѕ"}


@bp.route("/api/directions/photo/<token>", methods=["POST"])
def upload_direction_photo(token):
    """
    API РґР»СЏ Р·Р°РіСЂСѓР·РєРё С„РѕС‚РѕРіСЂР°С„РёРё РЅР°РїСЂР°РІР»РµРЅРёСЏ
    СЃРїРѕР»СЊР·СѓРµС‚СЃСЏ Р±РѕС‚РѕРј РїСЂРё РїРѕР»СѓС‡РµРЅРёРё С„РѕС‚РѕРіСЂР°С„РёРё РѕС‚ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°
    """
    db = g.db

    current_app.logger.info("direction photo upload start token=%s", token)

    session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
    if not session:
        current_app.logger.warning("direction upload: session not found token=%s", token)
        return {"error": "РЎРµСЃСЃРёСЏ РЅРµ РЅР°Р№РґРµРЅР°"}, 404

    if "photo" not in request.files:
        current_app.logger.warning("direction upload: no file provided token=%s", token)
        return {"error": "Р¤Р°Р№Р» РЅРµ Р·Р°РіСЂСѓР¶РµРЅ"}, 400

    file = request.files["photo"]
    if file.filename == "":
        current_app.logger.warning("direction upload: empty filename token=%s", token)
        return {"error": "Р¤Р°Р№Р» РЅРµ РІС‹Р±СЂР°РЅ"}, 400

    try:
        # РЎРѕС…СЂР°РЅСЏРµРј РІ var/media/directions/<session_id>/photo_xxx.ext
        directions_dir = MEDIA_ROOT / "directions" / str(session.session_id)
        os.makedirs(directions_dir, exist_ok=True)

        # РЎРѕС…СЂР°РЅСЏРµРј С„Р°Р№Р» (СЂР°СЃС€РёСЂРµРЅРёРµ Р±РµСЂРµРј РёР· mimetype/РёРјРµРЅРё С„Р°Р№Р»Р°)
        mime = (getattr(file, "mimetype", "") or "").lower()
        orig_ext = os.path.splitext(file.filename or "")[1].lower()
        ext = orig_ext
        if mime in ("image/jpeg", "image/jpg"):
            ext = ".jpg"
        elif mime == "image/png":
            ext = ".png"
        elif mime == "image/webp":
            ext = ".webp"
        if not ext:
            return {"error": "РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїСЂРµРґРµР»РёС‚СЊ С‚РёРї С„Р°Р№Р»Р°"}, 400
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in {".jpg", ".png", ".webp"}:
            return {"error": "РџРѕРґРґРµСЂР¶РёРІР°СЋС‚СЃСЏ С‚РѕР»СЊРєРѕ JPG/PNG/WEBP"}, 400

        filename = secure_filename(f"photo_{session.session_id}{ext}")
        filepath = directions_dir / filename
        file.save(filepath)

        # РЎРѕС…СЂР°РЅСЏРµРј РїСѓС‚СЊ РІ Р‘Р” РѕС‚РЅРѕСЃРёС‚РµР»СЊРЅРѕ РєРѕСЂРЅСЏ РїСЂРѕРµРєС‚Р°
        relative_path = os.path.relpath(filepath, PROJECT_ROOT)
        session.image_path = relative_path
        session.status = "photo_received"
        db.commit()

        current_app.logger.info(
            "direction upload success session_id=%s path=%s",
            session.session_id,
            filepath,
        )

        return {
            "message": "Р¤РѕС‚РѕРіСЂР°С„РёСЏ Р·Р°РіСЂСѓР¶РµРЅР°",
            "session_id": session.session_id,
            "status": "photo_received",
            "image_path": _build_image_url(session.image_path),
        }, 200

    except Exception as exc:
        db.rollback()
        current_app.logger.exception("РћС€РёР±РєР° РїСЂРё Р·Р°РіСЂСѓР·РєРµ С„РѕС‚РѕРіСЂР°С„РёРё РЅР°РїСЂР°РІР»РµРЅРёСЏ: %s", exc)
        return {"error": f"Internal server error while saving photo: {exc}"}, 500


@bp.route("/api/system-settings/public", methods=["GET"])
def get_public_system_settings():
    db = g.db
    items = list_settings(db, public_only=True)
    db.commit()
    return jsonify({"items": items, "specs": list_setting_specs(public_only=True)})


@bp.route("/api/admin/system-settings", methods=["GET"])
def admin_get_system_settings():
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    db = g.db
    items = list_settings(db, public_only=False)
    db.commit()
    return jsonify({"items": items, "specs": list_setting_specs(public_only=False)})


@bp.route("/api/admin/system-settings/<path:key>", methods=["PUT"])
def admin_update_system_setting(key):
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    data = request.json or {}
    if "value" not in data:
        return {"error": "value is required"}, 400

    db = g.db
    staff = _get_current_staff(db)
    reason = data.get("reason")
    try:
        setting_payload = update_setting(
            db,
            key=key,
            raw_value=data.get("value"),
            changed_by_staff_id=(staff.id if staff else None),
            reason=reason,
            source="admin_api",
        )
    except KeyError as exc:
        return {"error": str(exc)}, 404
    except SettingValidationError as exc:
        return {"error": str(exc)}, 400

    db.commit()
    return jsonify({"setting": setting_payload})


@bp.route("/api/admin/system-settings/changes", methods=["GET"])
def admin_get_system_settings_changes():
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    key = (request.args.get("key") or "").strip() or None
    limit_raw = request.args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 100
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}, 400

    db = g.db
    items = list_setting_changes(db, key=key, limit=limit)
    return jsonify({"items": items})


@bp.route("/api/admin/group-abonements/<int:abonement_id>/activate", methods=["POST"])
def admin_activate_abonement(abonement_id):
    """
    РђРєС‚РёРІР°С†РёСЏ Р°Р±РѕРЅРµРјРµРЅС‚Р° Р°РґРјРёРЅРѕРј (РЅР°РїСЂРёРјРµСЂ, РїРѕСЃР»Рµ РѕРїР»Р°С‚С‹ РІ Telegram).
    РњРµРЅСЏРµС‚ СЃС‚Р°С‚СѓСЃ Р°Р±РѕРЅРµРјРµРЅС‚Р° РЅР° active Рё, РµСЃР»Рё РµСЃС‚СЊ СЃРІСЏР·Р°РЅРЅР°СЏ С‚СЂР°РЅР·Р°РєС†РёСЏ, СЃС‚Р°РІРёС‚ РµС‘ РІ paid.
    """
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "РђР±РѕРЅРµРјРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    # С‰РµРј СЃРІСЏР·Р°РЅРЅСѓСЋ РѕРїР»Р°С‚Сѓ
    payment = None
    if abonement.id:
        payment = (
            db.query(PaymentTransaction)
            .filter(
                PaymentTransaction.user_id == abonement.user_id,
                PaymentTransaction.meta.ilike(f"%\"abonement_id\": {abonement.id}%"),
            )
            .order_by(PaymentTransaction.created_at.desc())
            .first()
        )

    if payment and payment.status != "paid":
        payment.status = "paid"
        payment.paid_at = datetime.now()

    abonement.status = "active"
    db.commit()

    return {
        "status": "active",
        "abonement_id": abonement.id,
        "payment_id": payment.id if payment else None,
    }


@bp.route("/api/admin/clients/merge", methods=["POST"])
def admin_merge_clients():
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    note = (payload.get("note") or "").strip()

    try:
        source_user_id = _parse_user_id_for_merge(payload, "source_user_id")
        target_user_id = _parse_user_id_for_merge(payload, "target_user_id")
    except ValueError as exc:
        return {"error": str(exc)}, 400

    if source_user_id == target_user_id:
        return {"error": "source_user_id and target_user_id must be different"}, 400

    source_user = db.query(User).filter_by(id=source_user_id).first()
    if not source_user:
        return {"error": "source user not found"}, 404

    target_user = db.query(User).filter_by(id=target_user_id).first()
    if not target_user:
        return {"error": "target user not found"}, 404

    if source_user.telegram_id:
        return {"error": "source user must not have telegram_id"}, 409
    if not target_user.telegram_id:
        return {"error": "target user must have telegram_id"}, 409

    if not target_user.username and source_user.username:
        target_user.username = source_user.username
    if not target_user.phone and source_user.phone:
        target_user.phone = source_user.phone
    if not target_user.email and source_user.email:
        target_user.email = source_user.email
    if not target_user.birth_date and source_user.birth_date:
        target_user.birth_date = source_user.birth_date
    if not target_user.photo_path and source_user.photo_path:
        target_user.photo_path = source_user.photo_path

    if source_user.user_notes:
        target_user.user_notes = _append_merge_note(target_user.user_notes, source_user.user_notes)
    if source_user.staff_notes:
        target_user.staff_notes = _append_merge_note(target_user.staff_notes, source_user.staff_notes)

    moved_group_abonements = int(
        db.query(GroupAbonement)
        .filter(GroupAbonement.user_id == source_user_id)
        .update({GroupAbonement.user_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_payments = int(
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.user_id == source_user_id)
        .update({PaymentTransaction.user_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_booking_requests = int(
        db.query(BookingRequest)
        .filter(BookingRequest.user_id == source_user_id)
        .update({BookingRequest.user_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_individual_lessons = int(
        db.query(IndividualLesson)
        .filter(IndividualLesson.student_id == source_user_id)
        .update({IndividualLesson.student_id: target_user_id}, synchronize_session=False)
        or 0
    )
    moved_schedule_overrides = int(
        db.query(ScheduleOverrides)
        .filter(ScheduleOverrides.created_by_user_id == source_user_id)
        .update({ScheduleOverrides.created_by_user_id: target_user_id}, synchronize_session=False)
        or 0
    )

    attendance_result = _merge_attendance_rows(db, source_user_id, target_user_id)
    intentions_result = _merge_attendance_intentions_rows(db, source_user_id, target_user_id)
    reminders_result = _merge_attendance_reminders_rows(db, source_user_id, target_user_id)

    merged_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    merge_marker = f"Merged into user #{target_user_id} at {merged_at}"
    if note:
        merge_marker = f"{merge_marker}. Note: {note}"
    source_user.staff_notes = _append_merge_note(source_user.staff_notes, merge_marker)
    source_user.status = "inactive"
    source_user.telegram_id = None

    db.commit()

    return jsonify(
        {
            "ok": True,
            "source_user_id": source_user_id,
            "target_user_id": target_user_id,
            "moved": {
                "group_abonements": moved_group_abonements,
                "payment_transactions": moved_payments,
                "booking_requests": moved_booking_requests,
                "individual_lessons": moved_individual_lessons,
                "schedule_overrides": moved_schedule_overrides,
                "attendance": attendance_result,
                "attendance_intentions": intentions_result,
                "attendance_reminders": reminders_result,
            },
        }
    )


@bp.route("/api/admin/clients/<int:user_id>/sick-leave", methods=["POST"])
def admin_apply_client_sick_leave(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}

    try:
        date_from = _parse_iso_date(payload.get("date_from"), "date_from")
        date_to = _parse_iso_date(payload.get("date_to"), "date_to")
    except ValueError as exc:
        return {"error": str(exc)}, 400

    if date_to < date_from:
        return {"error": "date_to РЅРµ РјРѕР¶РµС‚ Р±С‹С‚СЊ СЂР°РЅСЊС€Рµ date_from"}, 400

    note = (payload.get("note") or "").strip()
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    staff = _get_current_staff(db)
    now = datetime.utcnow()
    range_key = f"{date_from.isoformat()}:{date_to.isoformat()}"
    sick_default_comment = f"Р‘РѕР»РµР»: {date_from.isoformat()} - {date_to.isoformat()}"
    extension_days = (date_to - date_from).days + 1

    schedules = (
        db.query(Schedule)
        .filter(
            Schedule.object_type == "group",
            Schedule.date.isnot(None),
            Schedule.date >= date_from,
            Schedule.date <= date_to,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .order_by(Schedule.date.asc(), Schedule.time_from.asc())
        .all()
    )

    affected_schedule_ids = []
    created_attendance = 0
    updated_attendance = 0
    refunded_credits = 0
    affected_abonement_ids = set()

    for schedule in schedules:
        group_id = _schedule_group_id(schedule)
        if not group_id:
            continue

        abonement = _resolve_group_active_abonement(db, user.id, group_id, schedule.date)
        if not abonement:
            continue
        affected_abonement_ids.add(abonement.id)

        attendance = db.query(Attendance).filter_by(schedule_id=schedule.id, user_id=user.id).first()
        if not attendance:
            attendance = Attendance(
                schedule_id=schedule.id,
                user_id=user.id,
                status="sick",
                abonement_id=abonement.id,
                marked_at=now,
                marked_by_staff_id=staff.id if staff else None,
                comment=note or sick_default_comment,
            )
            db.add(attendance)
            db.flush()
            created_attendance += 1
        else:
            if attendance.status != "sick":
                updated_attendance += 1
            attendance.status = "sick"
            attendance.marked_at = now
            attendance.marked_by_staff_id = staff.id if staff else None
            if not attendance.abonement_id:
                attendance.abonement_id = abonement.id
            if note:
                attendance.comment = note
            elif not attendance.comment:
                attendance.comment = sick_default_comment

        affected_schedule_ids.append(schedule.id)

        debit_exists = db.query(GroupAbonementActionLog.id).filter_by(
            attendance_id=attendance.id,
            action_type="debit_attendance",
        ).first()
        refund_exists = db.query(GroupAbonementActionLog.id).filter_by(
            attendance_id=attendance.id,
            action_type="sick_leave_refund",
        ).first()
        if not debit_exists or refund_exists:
            continue

        refund_abonement_id = attendance.abonement_id or abonement.id
        refund_abonement = db.query(GroupAbonement).filter_by(id=refund_abonement_id).first()
        if not refund_abonement or refund_abonement.balance_credits is None:
            continue

        refund_abonement.balance_credits += 1
        refunded_credits += 1
        db.add(
            GroupAbonementActionLog(
                abonement_id=refund_abonement.id,
                action_type="sick_leave_refund",
                credits_delta=1,
                reason="sick_leave",
                note=f"Р’РѕР·РІСЂР°С‚ Р·Р°РЅСЏС‚РёСЏ Р·Р° Р±РѕР»СЊРЅРёС‡РЅС‹Р№ ({range_key})",
                attendance_id=attendance.id,
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "user_id": user.id,
                        "schedule_id": schedule.id,
                    },
                    ensure_ascii=False,
                ),
            )
        )

    extended_abonements = 0
    for abonement_id in affected_abonement_ids:
        abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
        if not abonement or not abonement.valid_to:
            continue

        duplicate_extension = db.query(GroupAbonementActionLog.id).filter_by(
            abonement_id=abonement.id,
            action_type="sick_leave_extend",
            reason=range_key,
        ).first()
        if duplicate_extension:
            continue

        abonement.valid_to = abonement.valid_to + timedelta(days=extension_days)
        extended_abonements += 1
        db.add(
            GroupAbonementActionLog(
                abonement_id=abonement.id,
                action_type="sick_leave_extend",
                credits_delta=0,
                reason=range_key,
                note=f"РџСЂРѕРґР»РµРЅРёРµ Р°Р±РѕРЅРµРјРµРЅС‚Р° РЅР° {extension_days} РґРЅ. (Р±РѕР»СЊРЅРёС‡РЅС‹Р№)",
                actor_type="staff",
                actor_id=staff.id if staff else None,
                payload=json.dumps(
                    {
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "user_id": user.id,
                        "extension_days": extension_days,
                    },
                    ensure_ascii=False,
                ),
            )
        )

    db.commit()

    return {
        "ok": True,
        "user_id": user.id,
        "telegram_id": user.telegram_id,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "extension_days": extension_days,
        "affected_schedules": len(affected_schedule_ids),
        "created_attendance": created_attendance,
        "updated_attendance": updated_attendance,
        "refunded_credits": refunded_credits,
        "extended_abonements": extended_abonements,
    }, 200


@bp.route("/api/admin/clients/<int:user_id>/abonements", methods=["GET"])
def admin_get_client_abonements(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    items = (
        db.query(GroupAbonement)
        .filter_by(user_id=user.id, status="active")
        .order_by(GroupAbonement.created_at.desc())
        .all()
    )
    return jsonify(
        {
            "user": {"id": user.id, "telegram_id": user.telegram_id, "name": user.name},
            "items": [_serialize_client_abonement_for_admin(db, item) for item in items],
        }
    )


@bp.route("/api/admin/clients/<int:user_id>/attendance-calendar", methods=["GET"])
def admin_get_client_attendance_calendar(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    month_param = request.args.get("month")
    try:
        month_start = _parse_month_start(month_param)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1)

    schedules = (
        db.query(Schedule)
        .filter(
            Schedule.object_type == "group",
            Schedule.date.isnot(None),
            Schedule.date >= month_start,
            Schedule.date < month_end,
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
        )
        .order_by(Schedule.date.asc(), Schedule.time_from.asc())
        .all()
    )

    schedule_ids = [s.id for s in schedules]
    attendance_by_schedule_id = {}
    if schedule_ids:
        for row in db.query(Attendance).filter(
            Attendance.user_id == user.id,
            Attendance.schedule_id.in_(schedule_ids),
        ).all():
            attendance_by_schedule_id[row.schedule_id] = row

    group_ids = sorted({
        _schedule_group_id(s) for s in schedules
        if _schedule_group_id(s)
    })
    groups = {}
    directions = {}
    if group_ids:
        for g_row in db.query(Group).filter(Group.id.in_(group_ids)).all():
            groups[g_row.id] = g_row
            if g_row.direction_id:
                directions[g_row.direction_id] = None
        direction_ids = [d_id for d_id in directions.keys()]
        if direction_ids:
            for d_row in db.query(Direction).filter(Direction.direction_id.in_(direction_ids)).all():
                directions[d_row.direction_id] = d_row

    entries = []
    for schedule in schedules:
        group_id = _schedule_group_id(schedule)
        if not group_id:
            continue

        attendance = attendance_by_schedule_id.get(schedule.id)
        enrolled = bool(_resolve_group_active_abonement(db, user.id, group_id, schedule.date))
        if not enrolled and not attendance:
            continue

        mark_code = None
        mark_label = None
        status = attendance.status if attendance else "planned"
        if status in {"present", "late"}:
            mark_code = "Рџ"
            mark_label = "РџСЂРёС€РµР»"
        elif status == "absent":
            mark_code = "Рќ"
            mark_label = "РќРµСЏРІРєР°"
        elif status == "sick":
            mark_code = "Р‘"
            mark_label = "Р‘РѕР»СЊРЅРёС‡РЅС‹Р№"
        elif status == "planned":
            mark_code = None
            mark_label = "Р—Р°РїРёСЃР°РЅ"

        group = groups.get(group_id)
        direction = directions.get(group.direction_id) if group and group.direction_id else None
        entries.append(
            {
                "date": schedule.date.isoformat(),
                "schedule_id": schedule.id,
                "group_id": group_id,
                "group_name": group.name if group else None,
                "direction_title": direction.title if direction else None,
                "time_from": schedule.time_from.strftime("%H:%M") if schedule.time_from else None,
                "time_to": schedule.time_to.strftime("%H:%M") if schedule.time_to else None,
                "status": status,
                "mark_code": mark_code,
                "mark_label": mark_label,
            }
        )

    return jsonify(
        {
            "user": {"id": user.id, "telegram_id": user.telegram_id, "name": user.name},
            "month": month_start.strftime("%Y-%m"),
            "entries": entries,
            "legend": {
                "Рџ": "РџСЂРёС€РµР»",
                "Рќ": "РќРµСЏРІРєР°",
                "Р‘": "Р‘РѕР»СЊРЅРёС‡РЅС‹Р№",
            },
        }
    )


@bp.route("/api/admin/group-abonements/<int:abonement_id>/extend", methods=["POST"])
def admin_extend_group_abonement(abonement_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "РђР±РѕРЅРµРјРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"}, 404

    group = db.query(Group).filter_by(id=abonement.group_id).first()
    lessons_per_week = int(group.lessons_per_week) if group and group.lessons_per_week else None
    if not lessons_per_week or lessons_per_week <= 0:
        return {"error": "Р”Р»СЏ РіСЂСѓРїРїС‹ РЅРµ РЅР°СЃС‚СЂРѕРµРЅРѕ РєРѕР»РёС‡РµСЃС‚РІРѕ Р·Р°РЅСЏС‚РёР№ РІ РЅРµРґРµР»СЋ"}, 400

    weeks_raw = payload.get("weeks")
    lessons_raw = payload.get("lessons")
    if weeks_raw in (None, "") and lessons_raw in (None, ""):
        return {"error": "РЈРєР°Р¶РёС‚Рµ weeks РёР»Рё lessons"}, 400

    weeks = None
    lessons = None
    if weeks_raw not in (None, ""):
        try:
            weeks = int(weeks_raw)
        except (TypeError, ValueError):
            return {"error": "weeks РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С†РµР»С‹Рј С‡РёСЃР»РѕРј"}, 400
        if weeks <= 0:
            return {"error": "weeks РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ Р±РѕР»СЊС€Рµ 0"}, 400
    if lessons_raw not in (None, ""):
        try:
            lessons = int(lessons_raw)
        except (TypeError, ValueError):
            return {"error": "lessons РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С†РµР»С‹Рј С‡РёСЃР»РѕРј"}, 400
        if lessons <= 0:
            return {"error": "lessons РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ Р±РѕР»СЊС€Рµ 0"}, 400

    if weeks is None and lessons is not None:
        if lessons % lessons_per_week != 0:
            return {"error": f"lessons РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РєСЂР°С‚РµРЅ {lessons_per_week}"}, 400
        weeks = lessons // lessons_per_week
    elif lessons is None and weeks is not None:
        lessons = weeks * lessons_per_week
    else:
        expected_lessons = weeks * lessons_per_week
        if lessons != expected_lessons:
            return {"error": f"РќРµСЃРѕРѕС‚РІРµС‚СЃС‚РІРёРµ: РїСЂРё {weeks} РЅРµРґ. РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ {expected_lessons} Р·Р°РЅСЏС‚РёР№"}, 400

    note = (payload.get("note") or "").strip()
    staff = _get_current_staff(db)
    now = datetime.utcnow()

    abonement.balance_credits = int(abonement.balance_credits or 0) + lessons

    valid_to_base = abonement.valid_to if (abonement.valid_to and abonement.valid_to > now) else now
    abonement.valid_to = valid_to_base + timedelta(days=weeks * 7)

    db.add(
        GroupAbonementActionLog(
            abonement_id=abonement.id,
            action_type="manual_extend_abonement",
            credits_delta=lessons,
            reason=f"weeks={weeks};lessons={lessons}",
            note=note or f"РџСЂРѕРґР»РµРЅРёРµ Р°Р±РѕРЅРµРјРµРЅС‚Р°: +{weeks} РЅРµРґ. / +{lessons} Р·Р°РЅСЏС‚РёР№",
            actor_type="staff",
            actor_id=staff.id if staff else None,
            payload=json.dumps(
                {
                    "weeks": weeks,
                    "lessons": lessons,
                    "lessons_per_week": lessons_per_week,
                    "user_id": abonement.user_id,
                    "group_id": abonement.group_id,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()

    return jsonify(
        {
            "ok": True,
            "abonement": _serialize_client_abonement_for_admin(db, abonement),
            "applied": {
                "weeks": weeks,
                "lessons": lessons,
                "lessons_per_week": lessons_per_week,
            },
        }
    )


