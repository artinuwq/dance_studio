from flask import Flask, jsonify, send_from_directory, request, g, make_response
from datetime import date, time, datetime, timedelta
import os
import json
import hashlib
import hmac
import base64
import time as time_module
from werkzeug.utils import secure_filename
import logging
import uuid
import requests
from pathlib import Path
from sqlalchemy import or_
from werkzeug.exceptions import HTTPException

from dance_studio.db import get_session, Session
from dance_studio.db.models import (
    Schedule,
    News,
    User,
    Staff,
    Mailing,
    Base,
    Direction,
    DirectionUploadSession,
    Group,
    IndividualLesson,
    HallRental,
    TeacherWorkingHours,
    TeacherTimeOff,
    GroupAbonement,
    PaymentTransaction,
    BookingRequest,
)
from dance_studio.core.media_manager import save_user_photo, delete_user_photo
from dance_studio.core.permissions import has_permission
from dance_studio.core.tech_notifier import send_critical_sync
from dance_studio.core.booking_utils import (
    BOOKING_STATUS_LABELS,
    format_booking_message,
    build_booking_keyboard_data,
)
from dance_studio.core.tg_auth import validate_init_data
from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID, BOT_TOKEN, APP_SECRET_KEY

# Flask-Admin
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView

# Отключаем SSL/TLS ошибки в логах werkzeug
logging.getLogger('werkzeug').setLevel(logging.ERROR)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = str(PROJECT_ROOT / "frontend")
BASE_DIR = str(Path(__file__).resolve().parent)
VAR_ROOT = PROJECT_ROOT / "var"
MEDIA_ROOT = VAR_ROOT / "media"
ALLOWED_DIRECTION_TYPES = {"dance", "sport"}
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days sliding

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
app.secret_key = APP_SECRET_KEY


def _session_secret() -> bytes:
    return (app.secret_key or "change-me").encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(data: str) -> bytes:
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def issue_session_token(user: dict, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Создает HMAC-подписанный токен с данными пользователя и exp."""
    payload = {
        "user": user,
        "exp": int(time_module.time()) + ttl_seconds,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    sig = hmac.new(_session_secret(), payload_bytes, hashlib.sha256).hexdigest()
    return f"tgs.{_b64(payload_bytes)}.{sig}"


def validate_session_token(token: str):
    """Проверяет подпись и срок действия; возвращает payload dict или None."""
    if not token or not token.startswith("tgs."):
        return None
    try:
        _, payload_b64, sig = token.split(".", 2)
    except ValueError:
        return None
    try:
        payload_bytes = _b64decode(payload_b64)
    except Exception:
        return None
    expected_sig = hmac.new(_session_secret(), payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return None
    try:
        payload = json.loads(payload_bytes.decode())
    except Exception:
        return None
    if payload.get("exp") and int(time_module.time()) > int(payload["exp"]):
        return None
    return payload


def _get_init_data_from_request():
    """Extracts Telegram init_data from header, query or JSON body."""
    header_value = request.headers.get("X-Telegram-Init-Data")
    if header_value:
        return header_value
    if request.args.get("init_data"):
        return request.args["init_data"]
    if request.method in {"POST", "PUT", "PATCH"}:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict) and "init_data" in payload:
            return payload.get("init_data")
    return None


def _get_session_token_from_request():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    cookie_token = request.cookies.get("tg_session")
    if cookie_token:
        return cookie_token
    return None


def get_telegram_user(optional: bool = True):
    """
    Validates Telegram WebApp init_data and returns user dict.
    When optional=False returns error response tuple on failure.
    """
    init_data = _get_init_data_from_request()
    if not init_data:
        return ({"error": "init_data обязателен"}, 400) if not optional else None
    user = validate_init_data(init_data)
    if not user:
        return ({"error": "init_data недействителен"}, 401) if not optional else None
    return user

# ====== Проверка прав по telegram_id ======
def check_permission(telegram_id, permission):
    db = g.db
    staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff or not staff.position:
        return False
    staff_position = staff.position.strip().lower()
    return has_permission(staff_position, permission)


def require_permission(permission, allow_self_staff_id=None):
    telegram_id = None
    telegram_id = request.headers.get("X-Telegram-Id") or request.args.get("telegram_id")
    data = request.get_json(silent=True) if request.is_json else None
    if not telegram_id and data:
        telegram_id = data.get("actor_telegram_id")
    if not telegram_id and getattr(g, "telegram_user", None):
        telegram_id = g.telegram_user.get("id")

    if not telegram_id:
        return {"error": "telegram_id обязателен"}, 401

    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return {"error": "Неверный telegram_id"}, 400

    # bypass для владельцев / техадмина даже без записи в staff
    if TECH_ADMIN_ID and telegram_id == TECH_ADMIN_ID:
        return None
    if telegram_id in OWNER_IDS:
        return None

    if allow_self_staff_id is not None:
        db = g.db
        staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
        if staff and staff.id == allow_self_staff_id:
            return None

    if not check_permission(telegram_id, permission):
        return {"error": "Нет прав доступа"}, 403

    return None

# Настройка Flask-Admin
class AdminView(AdminIndexView):
    def is_accessible(self):
        return True  # TODO: добавить проверку прав доступа

admin = Admin(app, name='🩰 Dance Studio Admin', index_view=AdminView())

@app.errorhandler(Exception)
def handle_unhandled_exception(error):
    if isinstance(error, HTTPException):
        return error
    try:
        send_critical_sync(f"? Flask error: {type(error).__name__}: {error}")
    except Exception:
        pass
    return jsonify({"error": "Internal server error"}), 500

# Добавляем модели в админ-панель
class UserModelView(ModelView):
    column_list = ['id', 'name', 'telegram_id', 'username', 'status', 'phone', 'registered_at']
    column_searchable_list = ['name', 'username', 'telegram_id']
    column_filters = ['status', 'registered_at']
    form_columns = ['telegram_id', 'username', 'name', 'phone', 'email', 'status', 'user_notes', 'staff_notes']

class StaffModelView(ModelView):
    column_list = ['id', 'name', 'position', 'phone', 'telegram_id', 'status']
    column_searchable_list = ['name', 'position', 'telegram_id']
    column_filters = ['position', 'status']
    form_columns = ['name', 'phone', 'email', 'telegram_id', 'position', 'specialization', 'bio', 'teaches', 'status']

class NewsModelView(ModelView):
    column_list = ['id', 'title', 'status', 'created_at']
    column_searchable_list = ['title', 'content']
    column_filters = ['status', 'created_at']
    form_columns = ['title', 'content', 'status', 'photo_path']

class MailingModelView(ModelView):
    column_list = ['mailing_id', 'name', 'status', 'target_type', 'mailing_type', 'created_at']
    column_searchable_list = ['name', 'purpose']
    column_filters = ['status', 'mailing_type', 'target_type', 'created_at']
    form_columns = ['name', 'description', 'purpose', 'status', 'target_type', 'target_id', 'mailing_type', 'scheduled_at']

class ScheduleModelView(ModelView):
    column_list = ['id', 'title', 'teacher_id', 'date', 'start_time', 'end_time', 'status']
    column_searchable_list = ['title']
    column_filters = ['status', 'date']

class DirectionModelView(ModelView):
    column_list = ['direction_id', 'title', 'direction_type', 'base_price', 'is_popular', 'status', 'created_at']
    column_searchable_list = ['title', 'description']
    column_filters = ['direction_type', 'status', 'is_popular', 'created_at']
    form_columns = ['title', 'direction_type', 'description', 'base_price', 'image_path', 'is_popular', 'status']

class DirectionUploadSessionModelView(ModelView):
    column_list = ['session_id', 'admin_id', 'title', 'status', 'created_at']
    column_searchable_list = ['title', 'session_token']
    column_filters = ['status', 'created_at']
    form_columns = ['admin_id', 'title', 'description', 'base_price', 'image_path', 'status', 'session_token']

admin.add_view(UserModelView(User, Session()))
admin.add_view(StaffModelView(Staff, Session()))
admin.add_view(NewsModelView(News, Session()))
admin.add_view(MailingModelView(Mailing, Session()))
admin.add_view(ScheduleModelView(Schedule, Session()))
admin.add_view(DirectionModelView(Direction, Session()))
admin.add_view(DirectionUploadSessionModelView(DirectionUploadSession, Session()))

# Автоматическое управление сессиями
@app.before_request
def before_request():
    g.db = get_session()
    g.telegram_user = None
    # 1) проверяем сессионный токен
    session_token = _get_session_token_from_request()
    if session_token:
        payload = validate_session_token(session_token)
        if payload and payload.get("user"):
            g.telegram_user = payload["user"]
            g.new_session_token = issue_session_token(g.telegram_user)  # sliding refresh
            return
    # 2) fallback на init_data
    init_data = _get_init_data_from_request()
    if init_data:
        user = validate_init_data(init_data)
        if user:
            g.telegram_user = user
            g.new_session_token = issue_session_token(user)

@app.teardown_request
def teardown_request(exception):
    db = getattr(g, 'db', None)
    if db is not None:
        db.close()

@app.after_request
def refresh_session_cookie(response):
    token = getattr(g, "new_session_token", None)
    if token:
        response.set_cookie(
            "tg_session",
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=False,
            samesite="Lax",
        )
    return response


def format_schedule(s):
    """Форматирует расписание с информацией об учителе"""
    teacher_info = {}
    if s.teacher_staff:
        teacher_info = {
            "id": s.teacher_staff.id,
            "name": s.teacher_staff.name,
            "photo": s.teacher_staff.photo_path
        }
    
    return {
        "id": s.id,
        "title": s.title,
        "teacher_id": s.teacher_id,
        "teacher": teacher_info,
        "date": s.date.isoformat(),
        "start": str(s.start_time),
        "end": str(s.end_time)
    }


def format_schedule_v2(s):
    return {
        "id": s.id,
        "object_type": s.object_type,
        "object_id": s.object_id,
        "group_id": s.group_id,
        "teacher_id": s.teacher_id,
        "date": s.date.isoformat() if s.date else None,
        "time_from": str(s.time_from) if s.time_from else None,
        "time_to": str(s.time_to) if s.time_to else None,
        "status": s.status,
        "status_comment": s.status_comment,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "updated_by": s.updated_by
    }


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/bot-username")
def get_bot_username():
    """Возвращает имя бота для открытия чата"""
    try:
        # Получаем имя бота из бота если доступно
        from bot.bot import BOT_USERNAME_GLOBAL
        if BOT_USERNAME_GLOBAL:
            return jsonify({"bot_username": BOT_USERNAME_GLOBAL})
        
        # Fallback на конфиг
        from dance_studio.core.config import BOT_USERNAME
        return jsonify({"bot_username": BOT_USERNAME})
    except:
        return jsonify({"bot_username": "dance_studio_admin_bot"})


@app.route("/auth/telegram/validate", methods=["POST"])
def auth_telegram_validate():
    """Проверяет init_data от Telegram WebApp и возвращает данные пользователя."""
    user = get_telegram_user(optional=False)
    if isinstance(user, tuple):
        # error tuple returned
        return user
    token = issue_session_token(user)
    resp = jsonify({"ok": True, "user": user, "session_token": token})
    resp.set_cookie("tg_session", token, max_age=SESSION_TTL_SECONDS, httponly=True, secure=False, samesite="Lax")
    return resp


@app.route("/schedule")
def schedule():
    db = g.db
    data = db.query(Schedule).all()
    return jsonify([format_schedule(s) for s in data])


@app.route("/schedule/public")
def schedule_public():
    db = g.db
    mine_flag = request.args.get("mine")
    user = get_current_user_from_request(db)
    mine = str(mine_flag).lower() in {"1", "true", "yes", "y"} if mine_flag is not None else bool(user)

    query = db.query(Schedule).outerjoin(IndividualLesson, Schedule.object_id == IndividualLesson.id)\
                               .outerjoin(HallRental, Schedule.object_id == HallRental.id)

    # базовый фильтр по статусу
    query = query.filter(Schedule.status != "cancelled")

    if mine and user:
        query = query.filter(
            or_(
                Schedule.object_type == "group",
                (Schedule.object_type == "individual") & (IndividualLesson.student_id == user.id),
                (Schedule.object_type == "rental") & (HallRental.creator_type == "user") & (HallRental.creator_id == user.id),
            )
        )
    else:
        # публичная выдача только групп
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
                        if direction.image_path:
                            direction_image = "/" + direction.image_path.replace("\\", "/")
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
                "title": s.title or "Индивидуальное занятие",
                "teacher_name": teacher.name if teacher else None,
                "student_id": lesson.student_id if lesson else None,
                "status": s.status,
            })
        elif s.object_type == "rental":
            rental = db.query(HallRental).filter_by(id=s.object_id).first() if s.object_id else None
            entry.update({
                "title": s.title or "Аренда зала",
                "creator_id": rental.creator_id if rental else None,
                "creator_type": rental.creator_type if rental else None,
                "status": s.status,
            })
        else:
            entry["title"] = s.title

        result.append(entry)

    return jsonify(result)


@app.route("/schedule/v2", methods=["GET"])
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
            return {"error": "date_from должен быть в формате YYYY-MM-DD"}, 400
    if date_to:
        try:
            date_to_val = datetime.strptime(date_to, "%Y-%m-%d").date()
            query = query.filter(Schedule.date <= date_to_val)
        except ValueError:
            return {"error": "date_to должен быть в формате YYYY-MM-DD"}, 400

    if mine:
        user = get_current_user_from_request(db)
        if not user:
            return {"error": "Требуется авторизация Telegram (init_data или заголовок X-Telegram-Id)"}, 401
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


@app.route("/schedule", methods=["POST"])
def create_schedule():
    """
    Создает новое занятие
    """
    db = g.db
    data = request.json

    

    if not data.get("title") or not data.get("teacher_id") or not data.get("date") or not data.get("start_time") or not data.get("end_time"):
        return {"error": "title, teacher_id, date, start_time и end_time обязательны"}, 400
    
    teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
    if not teacher:
        return {"error": "Учитель не найден"}, 404
    
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


@app.route("/schedule/v2", methods=["POST"])
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
        return {"error": "object_type должен быть одним из: group, individual, rental"}, 400
    if not object_id:
        return {"error": "object_id обязателен"}, 400
    if not date_str or not time_from_str or not time_to_str:
        return {"error": "date, time_from, time_to обязательны"}, 400

    try:
        date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
        time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
    except ValueError:
        return {"error": "Неверный формат даты или времени"}, 400

    if time_from_val >= time_to_val:
        return {"error": "time_from должен быть меньше time_to"}, 400

    group_id = data.get("group_id")
    teacher_id = data.get("teacher_id")

    title = None
    if object_type == "group":
        group = db.query(Group).filter_by(id=object_id).first()
        if not group:
            return {"error": "Группа не найдена"}, 404
        group_id = group.id
        teacher_id = group.teacher_id
        title = group.name
    elif object_type == "individual":
        lesson = db.query(IndividualLesson).filter_by(id=object_id).first()
        if not lesson:
            return {"error": "Индивидуальное занятие не найдено"}, 404
        teacher_id = lesson.teacher_id
        title = "Индивидуальное занятие"
    elif object_type == "rental":
        rental = db.query(HallRental).filter_by(id=object_id).first()
        if not rental:
            return {"error": "Аренда не найдена"}, 404
        title = "Аренда зала"

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
            return {"error": "repeat_weekly_until должен быть в формате YYYY-MM-DD"}, 400
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


@app.route("/schedule/<int:schedule_id>", methods=["PUT"])
def update_schedule(schedule_id):
    """
    Обновляет существующее занятие
    """
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    
    data = request.json
    
    if data.get("title"):
        schedule.title = data["title"]
    if data.get("teacher_id"):
        teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
        if not teacher:
            return {"error": "Учитель не найден"}, 404
        schedule.teacher_id = data["teacher_id"]
    if data.get("date"):
        schedule.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    if data.get("start_time"):
        schedule.start_time = datetime.strptime(data["start_time"], "%H:%M").time()
    if data.get("end_time"):
        schedule.end_time = datetime.strptime(data["end_time"], "%H:%M").time()
    
    db.commit()
    
    return format_schedule(schedule)


@app.route("/schedule/v2/<int:schedule_id>", methods=["PUT"])
def update_schedule_v2(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    data = request.json or {}

    if "date" in data:
        try:
            schedule.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date должен быть в формате YYYY-MM-DD"}, 400
    if "time_from" in data:
        try:
            schedule.time_from = datetime.strptime(data["time_from"], "%H:%M").time()
        except ValueError:
            return {"error": "time_from должен быть в формате HH:MM"}, 400
    if "time_to" in data:
        try:
            schedule.time_to = datetime.strptime(data["time_to"], "%H:%M").time()
        except ValueError:
            return {"error": "time_to должен быть в формате HH:MM"}, 400
    if "status" in data:
        schedule.status = data["status"]
    if "status_comment" in data:
        schedule.status_comment = data["status_comment"]
    if "updated_by" in data:
        schedule.updated_by = data["updated_by"]

    db.commit()
    return format_schedule_v2(schedule)


@app.route("/schedule/<int:schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    """
    Удаляет занятие
    """
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    
    schedule.status = "deleted"
    db.commit()
    
    return {"ok": True, "message": "Занятие удалено"}


@app.route("/schedule/v2/<int:schedule_id>", methods=["DELETE"])
def delete_schedule_v2(schedule_id):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    schedule.status = "cancelled"
    schedule.status_comment = schedule.status_comment or "Отменено"
    db.commit()
    return {"ok": True, "message": "Занятие отменено"}


@app.route("/news/manage")
def get_all_news():
    """Получает все новости для управления (включая активные и архивированные)"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = db.query(News).filter(News.status.in_(["active", "archived"])).order_by(News.created_at.desc()).all()

    result = []
    for n in data:
        photo_url = None
        if n.photo_path:
            photo_url = "/" + n.photo_path.replace("\\", "/")
        
        result.append({
            "id": n.id,
            "title": n.title,
            "content": n.content,
            "photo_path": photo_url,
            "created_at": n.created_at.isoformat(),
            "status": n.status
        })
    
    return jsonify(result)


@app.route("/news", methods=["POST"])
def create_news():
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    if not data.get("title") or not data.get("content"):
        return {"error": "title и content обязательны"}, 400
    
    news = News(
        title=data["title"],
        content=data["content"]
    )
    db.add(news)
    db.commit()
    
    return {
        "id": news.id,
        "title": news.title,
        "content": news.content,
        "photo_path": news.photo_path,
        "created_at": news.created_at.isoformat()
    }, 201


@app.route("/news")
def get_news():
    """Получает только активные новости для главной страницы"""
    db = g.db
    data = db.query(News).filter_by(status="active").order_by(News.created_at.desc()).all()

    result = []
    for n in data:
        photo_url = None
        if n.photo_path:
            photo_url = "/" + n.photo_path.replace("\\", "/")
        
        result.append({
            "id": n.id,
            "title": n.title,
            "content": n.content,
            "photo_path": photo_url,
            "created_at": n.created_at.isoformat()
        })

    # ETag based on response payload so client can revalidate quickly
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    etag = f"\"{hashlib.sha256(payload.encode('utf-8')).hexdigest()}\""
    client_etag = request.headers.get("If-None-Match")
    if client_etag == etag:
        resp = make_response("", 304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return resp

    resp = make_response(jsonify(result))
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return resp



@app.route("/news/<int:news_id>/photo", methods=["POST"])
def upload_news_photo(news_id):
    """
    Загружает фото нля новости
    """
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Файл не предоставлен"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Файл не выбран"}, 400
    
    # Проверяем расширение
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Допустимые форматы: jpg, jpeg, png, gif"}, 400
    
    try:
        # Удаляем старое фото если существует
        if news.photo_path:
            delete_user_photo(news.photo_path)
        
        # Сохраняем новое фото в папку media
        file_data = file.read()
        filename = "photo." + file.filename.rsplit('.', 1)[1].lower()
        
        from dance_studio.core.media_manager import MEDIA_DIR
        news_dir = os.path.join(MEDIA_DIR, "news", str(news_id))
        os.makedirs(news_dir, exist_ok=True)
        
        file_path = os.path.join(news_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        # Формируем путь: var/media/news/{id}/photo.ext
        photo_path = f"var/media/news/{news_id}/{filename}"
        print(f"📸 Фото сохранено в: {file_path}")
        print(f"📸 Путь в БД: {photo_path}")
        
        news.photo_path = photo_path
        db.commit()
        
        return {
            "id": news.id,
            "photo_path": news.photo_path,
            "message": "Фото успешно загружено"
        }, 201
    
    except Exception as e:
        print(f"Ошибка при загружке фото: {e}")
        return {"error": str(e)}, 500
@app.route("/news/<int:news_id>", methods=["DELETE"])
def delete_news(news_id):
    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    news.status = "deleted"
    db.commit()
    
    return {"ok": True}


@app.route("/news/<int:news_id>/archive", methods=["PUT"])
def archive_news(news_id):
    """Архивирует новость (переводит в статус 'archived')"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    news.status = "archived"
    db.commit()
    
    return {"ok": True}


@app.route("/news/<int:news_id>/restore", methods=["PUT"])
def restore_news(news_id):
    """Восстанавливает новость из архива (переводит статус обратно в 'active')"""
    perm_error = require_permission("create_news")
    if perm_error:
        return perm_error

    db = g.db
    news = db.query(News).filter_by(id=news_id).first()
    
    if not news:
        return {"error": "Новость не найдена"}, 404
    
    news.status = "active"
    db.commit()
    
    return {"ok": True}


@app.route("/users", methods=["POST"])
def register_user():
    db = g.db
    data = request.json
    
    # Проверяем обязательные поля (только telegram_id и name)
    if not data.get("telegram_id") or not data.get("name"):
        return {"error": "telegram_id и name обязательны"}, 400
    
    # Проверяем, не существует ли пользователь
    existing_user = db.query(User).filter_by(telegram_id=data["telegram_id"]).first()
    if existing_user:
        return {"error": "Пользователь уже зарегистрирован"}, 409
    
    user = User(
        telegram_id=data["telegram_id"],
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


@app.route("/users/<int:telegram_id>", methods=["GET"])
def get_user(telegram_id):
    db = g.db
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
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


@app.route("/users/list/all")
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


@app.route("/users/<int:telegram_id>", methods=["PUT"])
def update_user(telegram_id):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    data = request.json
    
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


@app.route("/users/<int:telegram_id>/photo", methods=["POST"])
def upload_user_photo(telegram_id):
    """
    Загружает фото пользователя (только для персонала)
    Ожидает файл в form-data с ключом 'photo'
    """
    db = g.db
    
    # Проверяем, является ли пользователь персоналом
    staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff:
        return {"error": "Загрузка фото доступна только для персонала"}, 403
    
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Файл не предоставлен"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Файл не выбран"}, 400
    
    # Проверяем расширение
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Допустимые форматы: jpg, jpeg, png, gif"}, 400
    
    try:
        # Удаляем старое фото если существует
        if user.photo_path:
            delete_user_photo(user.photo_path)
        
        # Сохраняем новое фото
        file_data = file.read()
        filename = "profile." + file.filename.rsplit('.', 1)[1].lower()
        photo_path = save_user_photo(telegram_id, file_data, filename)
        
        if not photo_path:
            return {"error": "Ошибка при сохранении файла"}, 500
        
        # Обновляем БД
        user.photo_path = photo_path
        db.commit()
        
        return {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "photo_path": user.photo_path,
            "message": "Фото успешно загружено"
        }, 201
    
    except Exception as e:
        print(f"Ошибка при загрузке фото: {e}")
        return {"error": str(e)}, 500


@app.route("/users/<int:telegram_id>/photo", methods=["DELETE"])
def delete_user_photo_endpoint(telegram_id):
    """
    Удаляет фото пользователя (только для персонала)
    """
    db = g.db
    
    # Проверяем, является ли пользователь персоналом
    staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff:
        return {"error": "Удаление фото доступно только для персонала"}, 403
    
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
    if not user.photo_path:
        return {"error": "Фото не найдено"}, 404
    
    try:
        delete_user_photo(user.photo_path)
        user.photo_path = None
        db.commit()
        
        return {"ok": True, "message": "Фото удалено"}
    
    except Exception as e:
        print(f"Ошибка при удалении фото: {e}")
        return {"error": str(e)}, 500


@app.route("/media/<path:filename>")
def serve_media(filename):
    """
    Служит медиа файлы из var/media; fallback на старый database/media
    """
    var_path = MEDIA_ROOT / filename
    legacy_dir = PROJECT_ROOT / "database" / "media"
    legacy_path = legacy_dir / filename

    if var_path.exists():
        return send_from_directory(var_path.parent, var_path.name)
    if legacy_path.exists():
        return send_from_directory(legacy_dir, filename)
    return {"error": "file not found"}, 404


@app.route("/database/media/<path:filename>")
def serve_media_full(filename):
    """
    Альтернативный маршрут; поддерживает и var/media, и старый путь
    """
    var_path = MEDIA_ROOT / filename
    legacy_dir = PROJECT_ROOT / "database" / "media"
    legacy_path = legacy_dir / filename

    if var_path.exists():
        return send_from_directory(var_path.parent, var_path.name)
    if legacy_path.exists():
        return send_from_directory(legacy_dir, filename)
    return {"error": "file not found"}, 404


@app.route("/staff")
def get_all_staff():
    """
    Получить всех сотрудников
    """
    perm_error = require_permission("view_all_users")
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter_by(status="active").order_by(Staff.created_at.desc()).all()
    
    result = []
    for s in staff:
        # Получаем username из User если есть telegram_id
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


@app.route("/staff/check/<int:telegram_id>")
def check_staff_by_telegram(telegram_id):
    """
    Проверить является ли пользователем сотрудником.
    Если данные персонала неполные, подгружает данные из профиля пользователя (без сохранения в БД).
    """
    try:
        db = g.db
        staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
        
        if not staff:
            return jsonify({
                "is_staff": False,
                "staff": None
            })
        
        # Загружаем профиль пользователя для подстановки данных
        try:
            user = db.query(User).filter_by(telegram_id=telegram_id).first()
        except:
            user = None
        
        # Если данные персонала неполные, берем из профиля пользователя
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
        print(f"⚠️ Ошибка при проверке сотрудника: {e}")
        return jsonify({
            "is_staff": False,
            "staff": None
        })


@app.route("/user/<int:telegram_id>/photo")
def get_user_photo(telegram_id):
    """
    Получить фото, загруженное пользователем через бота
    """
    try:
        db = g.db
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        
        if not user or not user.staff_notes:
            return {"photo_data": None}, 404
        
        # staff_notes содержит base64 фото
        return {
            "photo_data": user.staff_notes
        }
    except Exception as e:
        print(f"⚠️ Ошибка при получении фото: {e}")
        return {"error": str(e)}, 500


@app.route("/staff", methods=["POST"])
def create_staff():
    """
    Создать новый профиль сотрудника.
    Обязательные поля: position, name (или telegram_id с профилем)
    Остальные опциональные.
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    # Получаем имя: либо из данных, либо из профиля пользователя
    staff_name = data.get("name")
    if not staff_name and data.get("telegram_id"):
        user = db.query(User).filter_by(telegram_id=data.get("telegram_id")).first()
        if user and user.name:
            staff_name = user.name
    
    if not staff_name or not data.get("position"):
        return {"error": "name (или telegram_id с профилем) и position обязательны"}, 400

    # Проверяем допустимые должности
    valid_positions = ["учитель", "администратор", "владелец", "тех. админ"]
    if data.get("position").lower() not in valid_positions:
        return {"error": f"Допустимые должности: {', '.join(valid_positions)}"}, 400

    notify_flag = data.get("notify", True)
    notify_user = str(notify_flag).strip().lower() in ["1", "true", "yes", "y", "on"]

    teaches_value = 0
    teaches_raw = normalize_teaches(data.get("teaches"))
    if teaches_raw is None:
        teaches_value = 1 if data.get("position").lower() == "учитель" else 0
    else:
        teaches_value = teaches_raw

    
    # Защита от дублей по telegram_id
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
                            "учитель": "👩‍🏫 Учитель",
                            "администратор": "📋 Администратор",
                            "владелец": "👑 Владелец",
                            "тех. админ": "⚙️ Технический администратор"
                        }

                        position_name = position_display.get(data["position"], data["position"])
                        message_text = (
                            f"🎉 Вы снова в команде!\n\n"
                            f"Вам назначена должность:\n"
                            f"<b>{position_name}</b>\n\n"
                            f"Добро пожаловать обратно!"
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
                    "message": "Персонал восстановлен",
                    "id": existing_staff.id,
                    "restored": True
                }, 200

            return {
                "error": "Пользователь с таким telegram_id уже существует",
                "existing_id": existing_staff.id
            }, 409
    
    staff = Staff(
        name=staff_name,
        phone=data.get("phone") or "+7 000 000 00 00",  # Телефон опциональный
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
    
    # Отправляем уведомление в Telegram если есть telegram_id
    if data.get("telegram_id") and notify_user:
        try:
            import requests
            from dance_studio.core.config import BOT_TOKEN
            
            position_display = {
                "учитель": "👩‍🏫 Учитель",
                "администратор": "📋 Администратор",
                "владелец": "👑 Владелец",
                "тех. админ": "⚙️ Технический администратор"
            }
            
            position_name = position_display.get(data["position"], data["position"])
            
            message_text = (
                f"🎉 Поздравляем!\n\n"
                f"Вы назначены на должность:\n"
                f"<b>{position_name}</b>\n\n"
                f"в студии танца LISSA DANCE!"
            )
            
            # Отправляем сообщение напрямую через Telegram API
            telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": data.get("telegram_id"),
                "text": message_text,
                "parse_mode": "HTML"
            }
            
            response = requests.post(telegram_api_url, json=payload, timeout=5)
            if response.status_code == 200:
                pass  # print(f"✅ Уведомление отправлено пользователю {data.get('telegram_id')}")
            else:
                pass  # print(f"⚠️ Ошибка при отправке уведомления: {response.text}")
                
        except Exception as e:
            pass  # print(f"⚠️ Ошибка при отправке уведомления: {e}")
    
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


@app.route("/staff/<int:staff_id>", methods=["GET"])
def get_staff(staff_id):
    """
    Получить информацию о сотруднике
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404

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


@app.route("/staff/update-from-telegram/<int:telegram_id>", methods=["PUT"])
def update_staff_from_telegram(telegram_id):
    """
    Обновляет имя и другие данные персонала из Telegram профиля
    """
    db = g.db
    data = request.json
    
    staff = db.query(Staff).filter_by(telegram_id=telegram_id).first()
    
    if not staff:
        return {"error": "Персонал не найден"}, 404
    
    if "first_name" in data:
        # Формируем полное имя из first_name и last_name
        name = data["first_name"]
        if data.get("last_name"):
            name += " " + data["last_name"]
        staff.name = name
    
    db.commit()
    
    return {
        "id": staff.id,
        "name": staff.name,
        "position": staff.position,
        "message": "Имя обновлено из Telegram"
    }


@app.route("/staff/<int:staff_id>", methods=["PUT"])
def update_staff(staff_id):
    """
    Обновить информацию о сотруднике
    """
    perm_error = require_permission("manage_staff", allow_self_staff_id=staff_id)
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
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
        valid_positions = ["Учитель", "администратор", "модератор", "владелец"]
        if data["position"] not in valid_positions:
            return {"error": f"Допустимые должности: {', '.join(valid_positions)}"}, 400
        staff.position = data["position"]
    if "specialization" in data:
        staff.specialization = data["specialization"]
    if "bio" in data:
        staff.bio = data["bio"]
    if "teaches" in data:
        actor_telegram_id = request.headers.get("X-Telegram-Id") or request.args.get("telegram_id")
        if not actor_telegram_id and data:
            actor_telegram_id = data.get("actor_telegram_id")
        try:
            actor_telegram_id = int(actor_telegram_id) if actor_telegram_id is not None else None
        except (TypeError, ValueError):
            return {"error": "Неверный telegram_id"}, 400
        actor_staff = None
        if actor_telegram_id is not None:
            actor_staff = db.query(Staff).filter_by(telegram_id=actor_telegram_id, status="active").first()
        allowed_positions = {"администратор", "владелец", "тех. админ"}
        actor_position = (actor_staff.position or "").strip().lower() if actor_staff else ""
        if actor_position not in allowed_positions:
            return {"error": "Нет прав на изменение поля teaches"}, 403
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


@app.route("/teacher-working-hours/<int:teacher_id>", methods=["GET"])
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


@app.route("/teacher-working-hours/<int:teacher_id>", methods=["PUT"])
def put_teacher_working_hours(teacher_id):
    perm_error = require_permission("manage_staff", allow_self_staff_id=teacher_id)
    if perm_error:
        return perm_error

    db = g.db
    data = request.json or {}
    items = data.get("items", [])
    if not isinstance(items, list):
        return {"error": "items должен быть списком"}, 400

    parsed_items = []
    for item in items:
        try:
            weekday = int(item.get("weekday"))
        except (TypeError, ValueError):
            return {"error": "weekday должен быть числом 0..6"}, 400
        if weekday < 0 or weekday > 6:
            return {"error": "weekday должен быть в диапазоне 0..6"}, 400

        time_from_str = item.get("time_from")
        time_to_str = item.get("time_to")
        if not time_from_str or not time_to_str:
            return {"error": "time_from и time_to обязательны"}, 400
        try:
            time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
            time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
        except ValueError:
            return {"error": "time_from и time_to должны быть в формате HH:MM"}, 400
        if time_from_val >= time_to_val:
            return {"error": "time_from должен быть меньше time_to"}, 400

        valid_from = item.get("valid_from")
        valid_to = item.get("valid_to")
        try:
            valid_from_val = datetime.strptime(valid_from, "%Y-%m-%d").date() if valid_from else None
            valid_to_val = datetime.strptime(valid_to, "%Y-%m-%d").date() if valid_to else None
        except ValueError:
            return {"error": "valid_from и valid_to должны быть в формате YYYY-MM-DD"}, 400

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

@app.route("/staff/<int:staff_id>", methods=["DELETE"])
def delete_staff(staff_id):
    """
    Удалить сотрудника
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error

    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
    staff_name = staff.name
    telegram_id = staff.telegram_id

    # Вместо физического удаления — деактивируем, чтобы не ломать расписание
    staff.status = "dismissed"
    staff.teaches = 0
    db.commit()
    
    notify_flag = request.args.get("notify", "1").strip().lower()
    notify_user = notify_flag in ["1", "true", "yes", "y", "on"]

    # Отправляем уведомление об увольнении в Telegram если есть telegram_id
    if telegram_id and notify_user:
        try:
            import requests
            from dance_studio.core.config import BOT_TOKEN
            
            message_text = (
                f"😢 К сожалению...\n\n"
                f"Вы удалены из персонала студии танца LISSA DANCE.\n\n"
                f"Спасибо за сотрудничество!"
            )
            
            # Отправляем сообщение напрямую через Telegram API
            telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": telegram_id,
                "text": message_text,
                "parse_mode": "HTML"
            }
            
            response = requests.post(telegram_api_url, json=payload, timeout=5)
            if response.status_code == 200:
                pass  # print(f"✅ Уведомление об увольнении отправлено пользователю {telegram_id}")
            else:
                pass  # print(f"⚠️ Ошибка при отправке уведомления: {response.text}")
                
        except Exception as e:
            pass  # print(f"⚠️ Ошибка при отправке уведомления об увольнении: {e}")
    
    return {
        "message": f"Персонал '{staff_name}' удален",
        "deleted_id": staff_id,
        "status": staff.status
    }


@app.route("/staff/<int:staff_id>/photo", methods=["POST"])
def upload_staff_photo(staff_id):
    """
    Загружает фото сотрудника
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
    if 'photo' not in request.files:
        return {"error": "Файл не предоставлен"}, 400
    
    file = request.files['photo']
    
    if file.filename == '':
        return {"error": "Файл не выбран"}, 400
    
    # Проверяем расширение
    allowed_extensions = {'jpg', 'jpeg', 'png', 'gif'}
    if not ('.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return {"error": "Допустимые форматы: jpg, jpeg, png, gif"}, 400
    
    try:
        # Удаляем старое фото если существует
        if staff.photo_path:
            delete_user_photo(staff.photo_path)
        
        # Сохраняем новое фото в папку teachers
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
            "message": "Фото успешно загружено"
        }, 201
    
    except Exception as e:
        print(f"Ошибка при загрузке фото: {e}")
        return {"error": str(e)}, 500


@app.route("/staff/<int:staff_id>/photo", methods=["DELETE"])
def delete_staff_photo(staff_id):
    """
    Удаляет фото сотрудника
    """
    db = g.db
    staff = db.query(Staff).filter_by(id=staff_id).first()
    
    if not staff:
        return {"error": "Сотрудник не найден"}, 404
    
    if not staff.photo_path:
        return {"error": "Фото не найдено"}, 404
    
    try:
        delete_user_photo(staff.photo_path)
        staff.photo_path = None
        db.commit()
        
        return {"ok": True, "message": "Фото удалено"}
    
    except Exception as e:
        print(f"Ошибка при удалении фото: {e}")
        return {"error": str(e)}, 500


@app.route("/api/teachers", methods=["GET"])
def list_public_teachers():
    db = g.db
    teachers = db.query(Staff).filter(
        Staff.status == "active",
        or_(
            Staff.teaches == 1,
            (Staff.position.in_(["учитель", "Учитель"]) & Staff.teaches.is_(None))
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


@app.route("/api/teachers/<int:teacher_id>", methods=["GET"])
def get_public_teacher(teacher_id):
    db = g.db
    teacher = (
        db.query(Staff)
        .filter(
            Staff.id == teacher_id,
            Staff.status == "active",
            or_(
                Staff.teaches == 1,
                (Staff.position.in_(["учитель", "Учитель"]) & Staff.teaches.is_(None))
            )
        )
        .first()
    )
    if not teacher:
        return {"error": "Преподаватель не найден"}, 404
    return {
        "id": teacher.id,
        "name": teacher.name,
        "position": teacher.position,
        "specialization": teacher.specialization,
        "bio": teacher.bio,
        "photo": teacher.photo_path,
    }


@app.route("/api/teachers/<int:teacher_id>/schedule", methods=["GET"])
def get_public_teacher_schedule(teacher_id):
    db = g.db
    teacher_exists = db.query(Staff).filter(
        Staff.id == teacher_id,
        Staff.status == "active",
        or_(
            Staff.teaches == 1,
            (Staff.position.in_(["учитель", "Учитель"]) & Staff.teaches.is_(None))
        )
    ).first()
    if not teacher_exists:
        return {"error": "Преподаватель не найден"}, 404
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


@app.route("/api/teachers/<int:teacher_id>/availability", methods=["GET"])
def get_teacher_availability(teacher_id):
    db = g.db
    teacher = db.query(Staff).filter(Staff.id == teacher_id, Staff.status == "active").first()
    if not teacher:
        return {"error": "Преподаватель не найден"}, 404

    start_str = request.args.get("start")
    days_str = request.args.get("days")
    duration_str = request.args.get("duration")
    step_str = request.args.get("step")

    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else date.today()
    except ValueError:
        return {"error": "start должен быть в формате YYYY-MM-DD"}, 400

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


@app.route("/staff/list/all")
def list_all_staff():
    """
    Возвращает список всего персонала для администраторов
    """
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    staff = db.query(Staff).filter(Staff.status != "dismissed").all()
    
    result = []
    for s in staff:
        # Получаем username из User если есть telegram_id
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


@app.route("/staff/search")
def search_staff():
    """
    Поиск пользователей для добавления в персонал.
    Параметры query:
    - q: строка поиска (если не указана, возвращает всех пользователей)
    - by_username: если True, ищет только по юзернейму (используется при @username)
    """
    try:
        db = g.db
        search_query = request.args.get('q', '').strip().lower()
        by_username = request.args.get('by_username', 'false').lower() == 'true'
        
        # Ищем среди пользователей (Users), а не среди персонала (Staff)
        users = db.query(User).all()
        result = []
        
        # Если нет поискового запроса, возвращаем всех пользователей
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
            # Выполняем фильтр в зависимости от типа поиска
            for u in users:
                if by_username:
                    # Поиск только по юзернейму (при вводе @username)
                    if u.username:
                        # Нормализуем: убираем @ из обоих строк для сравнения
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
                    # Поиск по имени или telegram_id (при обычном вводе)
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
        print(f"Ошибка при поиске пользователей: {e}")
        return jsonify({"error": str(e)}), 500


# ======================== СИСТЕМА РАССЫЛОК ========================

@app.route("/search-users")
def search_users():
    """Поиск пользователей для рассылок"""
    db = g.db
    try:
        search_query = request.args.get('query', '').strip().lower()
        
        if not search_query:
            return jsonify([]), 200
        
        users = db.query(User).all()
        result = []
        
        for u in users:
            # Поиск по имени или telegram_id
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
        print(f"Ошибка при поиске пользователей: {e}")
        return jsonify({"error": str(e)}), 500
@app.route("/mailings", methods=["GET"])
def get_mailings():
    """Получает все рассылки (для управления)"""
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
        print(f"Ошибка при получении рассылок: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/mailings", methods=["POST"])
def create_mailing():
    """Создает новую рассылку"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    try:
        # Обязательные поля
        if not data.get("creator_id") or not data.get("name") or not data.get("purpose") or not data.get("target_type"):
            return {"error": "creator_id, name, purpose и target_type обязательны"}, 400
        
        # Определяем статус на основе выбора пользователя
        send_now = data.get("send_now", False)
        
        # Если отправляем сейчас, статус = "pending" (ждет отправки)
        # Если отправляем позже, статус = "scheduled"
        status = "pending" if send_now else "scheduled"
        
        # Если нужно отправить сейчас
        sent_at = None
        if send_now:
            sent_at = None  # Отправляется в процессе, sent_at установится после отправки
        
        scheduled_at = data.get("scheduled_at")
        
        # Если это отложенная рассылка, нужно время
        if not send_now and not scheduled_at:
            return {"error": "Для отложенной рассылки требуется scheduled_at"}, 400
        
        # Если scheduled_at передана как строка, конвертируем в datetime
        if scheduled_at and isinstance(scheduled_at, str):
            # Убеждаемся что есть секунды в строке (datetime-local может их не содержать)
            if 'T' in scheduled_at and scheduled_at.count(':') == 1:
                scheduled_at = scheduled_at + ':00'  # Добавляем :00 для секунд
            try:
                scheduled_at = datetime.fromisoformat(scheduled_at)
            except ValueError as e:
                return {"error": f"Неверный формат даты: {e}"}, 400
        
        mailing = Mailing(
            creator_id=data["creator_id"],
            name=data["name"],
            description=data.get("description"),
            purpose=data["purpose"],
            status=status,
            target_type=data["target_type"],
            target_id=data.get("target_id"),
            mailing_type=data.get("mailing_type", "manual"),  # По умолчанию - ручная рассылка
            sent_at=sent_at,
            scheduled_at=scheduled_at
        )
        
        db.add(mailing)
        db.commit()
        
        # Если нужно отправить сейчас, добавляем в очередь отправки
        if send_now:
            from bot.bot import queue_mailing_for_sending
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
        print(f"Ошибка при создании рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>", methods=["GET"])
def get_mailing(mailing_id):
    """Получает информацию о конкретной рассылке"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
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
        print(f"Ошибка при получении рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>", methods=["PUT"])
def update_mailing(mailing_id):
    """Обновляет рассылку"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    data = request.json
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Обновляем поля
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
        print(f"Ошибка при обновлении рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>", methods=["DELETE"])
def delete_mailing(mailing_id):
    """Удаляет рассылку (или отменяет её)"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    db = g.db
    
    try:
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Устанавливаем статус "отменено" вместо удаления
        mailing.status = "cancelled"
        db.commit()
        
        return {"message": "Рассылка отменена"}, 200
    
    except Exception as e:
        db.rollback()
        print(f"Ошибка при удалении рассылки: {e}")
        return {"error": str(e)}, 500


@app.route("/mailings/<int:mailing_id>/send", methods=["POST"])
def send_mailing_endpoint(mailing_id):
    """Инициирует отправку рассылки"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    try:
        # Импортируем функцию добавления рассылки в очередь
        from bot.bot import queue_mailing_for_sending
        
        db = g.db
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        
        if not mailing:
            return {"error": "Рассылка не найдена"}, 404
        
        # Проверяем, не отправлена ли уже
        if mailing.status == "sent":
            return {"error": "Рассылка уже была отправлена"}, 400
        
        if mailing.status == "cancelled":
            return {"error": "Рассылка была отменена"}, 400
        
        # Добавляем рассылку в очередь на отправку
        queue_mailing_for_sending(mailing_id)
        
        return {"message": f"Рассылка '{mailing.name}' добавлена в очередь отправки", "status": "pending"}, 200
    
    except Exception as e:
        print(f"Ошибка при отправке рассылки: {e}")
        return {"error": str(e)}, 500


# ======================== СИСТЕМА УПРАВЛЕНИЯ НАПРАВЛЕНИЯМИ ========================

@app.route("/api/directions", methods=["GET"])
def get_directions():
    """Получает все активные направления"""
    db = g.db
    direction_type = request.args.get("direction_type") or request.args.get("type")
    query = db.query(Direction).filter_by(status="active")
    if direction_type:
        direction_type = direction_type.lower()
        if direction_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
        query = query.filter(Direction.direction_type == direction_type)

    directions = query.order_by(Direction.created_at.desc()).all()

    #print(f"✓ Найдено {len(directions)} активных направлений")
    
    result = []
    for d in directions:
        image_url = None
        if d.image_path:
            image_url = "/" + d.image_path.replace("\\", "/")
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


@app.route("/api/directions/manage", methods=["GET"])
def get_directions_manage():
    """Получает все направления для управления (включая неактивные)"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    direction_type = request.args.get("direction_type") or request.args.get("type")
    query = db.query(Direction)
    if direction_type:
        direction_type = direction_type.lower()
        if direction_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
        query = query.filter(Direction.direction_type == direction_type)

    directions = query.order_by(Direction.created_at.desc()).all()
    
    result = []
    for d in directions:
        image_url = None
        if d.image_path:
            image_url = "/" + d.image_path.replace("\\", "/")
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


@app.route("/api/directions/<int:direction_id>", methods=["GET"])
def get_direction(direction_id):
    """Возвращает одно направление по ID для формы редактирования"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    image_url = None
    if direction.image_path:
        image_url = "/" + direction.image_path.replace("\\", "/")

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


@app.route("/api/directions/<int:direction_id>/groups", methods=["GET"])
def get_direction_groups(direction_id):
    """Возвращает список групп для направления"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

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


@app.route("/api/directions/<int:direction_id>/groups", methods=["POST"])
def create_direction_group(direction_id):
    """Создает группу внутри направления"""
    perm_error = require_permission("create_group")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}

    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

    name = data.get("name")
    teacher_id = data.get("teacher_id")
    age_group = data.get("age_group")
    max_students = data.get("max_students")
    duration_minutes = data.get("duration_minutes")
    lessons_per_week = data.get("lessons_per_week")
    description = data.get("description")

    if not name or not teacher_id or not age_group or not max_students or not duration_minutes:
        return {"error": "name, teacher_id, age_group, max_students, duration_minutes обязательны"}, 400

    teacher = db.query(Staff).filter_by(id=teacher_id).first()
    if not teacher:
        return {"error": "Преподаватель не найден"}, 404

    try:
        max_students_int = int(max_students)
        duration_minutes_int = int(duration_minutes)
    except ValueError:
        return {"error": "max_students и duration_minutes должны быть числами"}, 400

    lessons_per_week_int = None
    if lessons_per_week is not None and lessons_per_week != "":
        try:
            lessons_per_week_int = int(lessons_per_week)
        except ValueError:
            return {"error": "lessons_per_week должен быть числом"}, 400

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

    # Создаем чат Telegram через userbot и добавляем преподавателя
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

            # Всегда шлём ссылку преподавателю, даже если invite сработал — на случай приватности.
            target_ids = {teacher.telegram_id} | {uid for uid in failed if uid}
            for uid in target_ids:
                try:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": int(uid),
                            "text": f"Присоединиться к чату группы \"{name}\" можно по ссылке: {group.chat_invite_link}",
                            "disable_web_page_preview": True,
                        },
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        print(f"[create_direction_group] sendMessage to {uid} failed: {resp.status_code} {resp.text}")
                except Exception as send_err:
                    print(f"[create_direction_group] Не удалось отправить ссылку пользователю {uid}: {send_err}")
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


@app.route("/api/groups/<int:group_id>", methods=["GET"])
def get_group(group_id):
    """Возвращает группу по ID"""
    db = g.db
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Группа не найдена"}, 404

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


@app.route("/api/groups/<int:group_id>", methods=["PUT"])
def update_group(group_id):
    """Обновляет группу"""
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Группа не найдена"}, 404

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
            return {"error": "max_students должен быть числом"}, 400
    if "duration_minutes" in data:
        try:
            group.duration_minutes = int(data["duration_minutes"])
        except ValueError:
            return {"error": "duration_minutes должен быть числом"}, 400
    if "lessons_per_week" in data:
        if data["lessons_per_week"] in (None, ""):
            group.lessons_per_week = None
        else:
            try:
                group.lessons_per_week = int(data["lessons_per_week"])
            except ValueError:
                return {"error": "lessons_per_week должен быть числом"}, 400
    if "teacher_id" in data:
        teacher = db.query(Staff).filter_by(id=data["teacher_id"]).first()
        if not teacher:
            return {"error": "Преподаватель не найден"}, 404
        group.teacher_id = data["teacher_id"]

    db.commit()

    return {
        "id": group.id,
        "message": "Группа обновлена"
    }


def normalize_teaches(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value else 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "y", "да"):
            return 1
        if v in ("0", "false", "no", "n", "нет"):
            return 0
    return None


def try_fetch_telegram_avatar(telegram_id, db, staff_obj=None):
    """Пробует скачать аватар пользователя из Telegram и сохранить в БД"""
    try:
        from dance_studio.core.config import BOT_TOKEN
    except Exception:
        return

    try:
        # Получаем фото профиля
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
            params={"user_id": telegram_id, "limit": 1},
            timeout=5
        )
        data = resp.json()
        if not data.get("ok") or data.get("result", {}).get("total_count", 0) == 0:
            return

        file_id = data["result"]["photos"][0][-1]["file_id"]
        file_resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=5
        )
        file_data = file_resp.json()
        if not file_data.get("ok"):
            return

        file_path = file_data["result"]["file_path"]
        photo_resp = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
            timeout=10
        )
        if photo_resp.status_code != 200:
            return

        photo_path = save_user_photo(telegram_id, photo_resp.content)
        if not photo_path:
            return

        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user and not user.photo_path:
            user.photo_path = photo_path

        if staff_obj and not staff_obj.photo_path:
            staff_obj.photo_path = photo_path

        db.commit()
    except Exception:
        # Без падения сервера при ошибке сети
        return


@app.route("/api/directions/create-session", methods=["POST"])
def create_direction_upload_session():
    """
    Создает сессию загрузки направления.
    Администратор заполняет форму и получает токен для бота.
    """
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json or {}
    
    telegram_user = getattr(g, "telegram_user", None)
    telegram_user_id = None
    if isinstance(telegram_user, dict) and telegram_user.get("id"):
        telegram_user_id = telegram_user["id"]
    else:
        allow_dev = os.getenv("ALLOW_DEV_TELEGRAM_ID_AUTH", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "y",
            "on",
        )
        if allow_dev:
            telegram_user_id = request.headers.get("X-Telegram-Id") or data.get("telegram_user_id")
        if not telegram_user_id:
            return {"error": "init_data требуется для аутентификации"}, 401

    try:
        telegram_user_id = int(telegram_user_id)
    except (TypeError, ValueError):
        return {"error": "Неверный telegram_id"}, 400

    admin = db.query(Staff).filter_by(telegram_id=telegram_user_id).first()
    if not admin or admin.position not in ["администратор", "владелец", "тех. админ"]:
        return {"error": "У вас нет прав администратора"}, 403
    
    # Обязательные поля
    required_fields = ["title", "description", "base_price"]
    for field in required_fields:
        if not data.get(field):
            return {"error": f"{field} обязателен"}, 400

    direction_type = (data.get("direction_type") or "dance").lower()
    if direction_type not in ALLOWED_DIRECTION_TYPES:
        return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
    
    # Создаем сессию
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
        "message": "Сессия создана. Отправьте токен боту для загрузки фотографии."
    }, 201


@app.route("/api/directions/upload-complete/<token>", methods=["GET"])
def get_upload_session_status(token):
    """Проверяет статус загрузки фотографии по токену"""
    db = g.db
    
    session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
    if not session:
        print(f"❌ Сессия не найдена для токена: {token}")
        return {"error": "Сессия не найдена"}, 404
    
    print(f"✓ Статус сессии {token[:8]}...: {session.status}, фото: {session.image_path}")
    
    return {
        "session_id": session.session_id,
        "status": session.status,
        "direction_type": session.direction_type or "dance",
        "image_path": "/" + session.image_path.replace("\\", "/") if session.image_path else None,
        "title": session.title,
        "description": session.description,
        "base_price": session.base_price
    }


@app.route("/api/directions", methods=["POST"])
def create_direction():
    """Создает направление после загрузки фото ботом"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json

    print(f"[create_direction] request: {data}")

    session_token = data.get("session_token")
    if not session_token:
        return {"error": "session_token ??????????"}, 400

    session = db.query(DirectionUploadSession).filter_by(session_token=session_token).first()
    if not session:
        print(f"[create_direction] session not found: {session_token}")
        return {"error": "?????? ?? ???????"}, 404

    print(f"[create_direction] session found: status={session.status}, photo={session.image_path}")

    if session.status != "photo_received":
        return {"error": f"Сессия не готова. Статус: {session.status}"}, 400

    direction_type = (data.get("direction_type") or session.direction_type or "dance").lower()
    if direction_type not in ALLOWED_DIRECTION_TYPES:
        return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400

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
        "message": "??????????? ??????? ???????"
    }, 201


@app.route("/api/directions/<int:direction_id>", methods=["PUT"])
def update_direction(direction_id):
    """Обновляет информацию о направлении"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    data = request.json
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404
    
    # Обновляем поля
    if "title" in data:
        direction.title = data["title"]
    if "description" in data:
        direction.description = data["description"]
    if "base_price" in data:
        direction.base_price = data["base_price"]
    if "direction_type" in data:
        new_type = (data.get("direction_type") or "").lower()
        if new_type not in ALLOWED_DIRECTION_TYPES:
            return {"error": "direction_type должен быть 'dance' или 'sport'"}, 400
        direction.direction_type = new_type
    if "status" in data:
        direction.status = data["status"]
    if "is_popular" in data:
        direction.is_popular = data["is_popular"]
    
    db.commit()
    
    return {
        "direction_id": direction.direction_id,
        "direction_type": direction.direction_type,
        "message": "Направление обновлено"
    }


@app.route("/api/directions/<int:direction_id>", methods=["DELETE"])
def delete_direction(direction_id):
    """Удаляет направление"""
    perm_error = require_permission("create_direction")
    if perm_error:
        return perm_error
    db = g.db
    
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404
    
    direction.status = "inactive"
    db.commit()
    
    return {"message": "Направление удалено"}


@app.route("/api/directions/photo/<token>", methods=["POST"])
def upload_direction_photo(token):
    """
    API для загрузки фотографии направления
    Используется ботом при получении фотографии от администратора
    """
    db = g.db
    
    print(f"🔄 Загрузка фотографии для токена: {token[:8]}...")
    
    session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
    if not session:
        print(f"❌ Сессия не найдена: {token}")
        return {"error": "Сессия не найдена"}, 404
    
    if "photo" not in request.files:
        print(f"❌ Файл не загружен")
        return {"error": "Файл не загружен"}, 400
    
    file = request.files["photo"]
    if file.filename == "":
        print(f"❌ Файл не выбран")
        return {"error": "Файл не выбран"}, 400
    
    try:
        # Создаем директорию для направлений если её нет
        # PROJECT_ROOT = BASE_DIR/.., где BASE_DIR это папка backend
        project_root = os.path.dirname(BASE_DIR)
        directions_dir = os.path.join(project_root, "database", "media", "directions", str(session.session_id))
        os.makedirs(directions_dir, exist_ok=True)
        
        print(f"✓ Директория создана: {directions_dir}")
        
        # Сохраняем файл (расширение берем из mimetype/имени файла)
        mime = (getattr(file, "mimetype", "") or "").lower()
        orig_ext = os.path.splitext(file.filename or "")[1].lower()
        ext = orig_ext
        if mime in ("image/jpeg", "image/jpg"):
            ext = ".jpg"
        elif mime == "image/png":
            ext = ".png"
        elif mime == "image/webp":
            ext = ".webp"
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in {".jpg", ".png", ".webp"}:
            return {"error": "Поддерживаются только JPG/PNG/WEBP"}, 400

        filename = secure_filename(f"photo_{session.session_id}{ext}")
        filepath = os.path.join(directions_dir, filename)
        file.save(filepath)
        
        print(f"✓ Файл сохранен: {filepath}")
        
        # Сохраняем путь в БД относительно корня проекта
        relative_path = os.path.relpath(filepath, project_root)
        session.image_path = relative_path
        session.status = "photo_received"
        db.commit()
        
        print(f"✅ Статус сессии обновлен на 'photo_received'")
        
        return {
            "message": "Фотография загружена",
            "session_id": session.session_id,
            "status": "photo_received",
            "image_path": "/" + session.image_path.replace("\\", "/") if session.image_path else None,
        }, 200
    
    except Exception as e:
        print(f"Ошибка при загрузке фотографии: {e}")
        return {"error": str(e)}, 500


# ======================== ГРУППОВЫЕ АБОНЕМЕНТЫ / ОПЛАТЫ (ЗАГЛУШКА) ========================
def get_current_user_from_request(db):
    if getattr(g, "telegram_user", None) and g.telegram_user.get("id"):
        try:
            telegram_id = int(g.telegram_user["id"])
            return db.query(User).filter_by(telegram_id=telegram_id).first()
        except (TypeError, ValueError):
            return None
    telegram_id = request.headers.get("X-Telegram-Id") or request.args.get("telegram_id")
    if not telegram_id:
        return None
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return None
    return db.query(User).filter_by(telegram_id=telegram_id).first()

def _time_overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a


def _compute_duration_minutes(time_from, time_to) -> int | None:
    if not time_from or not time_to:
        return None
    delta = datetime.combine(date.today(), time_to) - datetime.combine(date.today(), time_from)
    minutes = int(delta.total_seconds() // 60)
    return minutes if minutes > 0 else None


def _find_booking_overlaps(db, date_val, time_from, time_to) -> list[dict]:
    overlaps = []

    schedules = db.query(Schedule).filter(
        Schedule.date == date_val,
        Schedule.status.notin_(["cancelled", "deleted"])
    ).all()
    for item in schedules:
        start = item.time_from or item.start_time
        end = item.time_to or item.end_time
        if not start or not end:
            continue
        if _time_overlaps(time_from, time_to, start, end):
            overlaps.append({
                "date": date_val.strftime("%d.%m.%Y"),
                "time_from": start.strftime("%H:%M"),
                "time_to": end.strftime("%H:%M"),
                "title": item.title or "Занятие"
            })

    rentals = db.query(HallRental).filter_by(date=date_val).all()
    for item in rentals:
        if not item.time_from or not item.time_to:
            continue
        if _time_overlaps(time_from, time_to, item.time_from, item.time_to):
            overlaps.append({
                "date": date_val.strftime("%d.%m.%Y"),
                "time_from": item.time_from.strftime("%H:%M"),
                "time_to": item.time_to.strftime("%H:%M"),
                "title": "Аренда зала"
            })

    lessons = db.query(IndividualLesson).filter_by(date=date_val).all()
    for item in lessons:
        if not item.time_from or not item.time_to:
            continue
        if _time_overlaps(time_from, time_to, item.time_from, item.time_to):
            overlaps.append({
                "date": date_val.strftime("%d.%m.%Y"),
                "time_from": item.time_from.strftime("%H:%M"),
                "time_to": item.time_to.strftime("%H:%M"),
                "title": "Индивидуальное занятие"
            })

    return overlaps


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _minutes_to_time_str(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def _subtract_busy_intervals(start: int, end: int, busy_intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    current = start
    for busy_start, busy_end in busy_intervals:
        if busy_end <= current:
            continue
        if busy_start >= end:
            break
        if busy_start > current:
            segments.append((current, min(busy_start, end)))
        current = max(current, busy_end)
        if current >= end:
            break
    if current < end:
        segments.append((current, end))
    return segments


def _has_slot_conflict(start_min: int, duration_minutes: int, busy_intervals: list[tuple[int, int]]) -> bool:
    end_min = start_min + duration_minutes
    for busy_start, busy_end in busy_intervals:
        if start_min < busy_end and busy_start < end_min:
            return True
    return False


def _collect_busy_intervals(db, teacher_id: int, target_date: date) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []

    schedule_items = (
        db.query(Schedule)
        .filter(
            Schedule.teacher_id == teacher_id,
            Schedule.date == target_date,
            Schedule.status.notin_(["cancelled", "deleted"]),
        )
        .all()
    )
    for item in schedule_items:
        start = item.time_from or item.start_time
        end = item.time_to or item.end_time
        if not start or not end:
            continue
        start_min = _time_to_minutes(start)
        end_min = _time_to_minutes(end)
        if end_min <= start_min:
            continue
        intervals.append((start_min, end_min))

    lessons = db.query(IndividualLesson).filter_by(teacher_id=teacher_id, date=target_date).all()
    for lesson in lessons:
        if not lesson.time_from or not lesson.time_to:
            continue
        start_min = _time_to_minutes(lesson.time_from)
        end_min = _time_to_minutes(lesson.time_to)
        if end_min <= start_min:
            continue
        intervals.append((start_min, end_min))

    time_off_items = (
        db.query(TeacherTimeOff)
        .filter_by(teacher_id=teacher_id, date=target_date, status="active")
        .all()
    )
    for off in time_off_items:
        if off.time_from and off.time_to:
            start_min = _time_to_minutes(off.time_from)
            end_min = _time_to_minutes(off.time_to)
            if end_min > start_min:
                intervals.append((start_min, end_min))
        else:
            intervals.append((0, 24 * 60))

    return intervals


def _notify_booking_admins(booking: BookingRequest, user: User) -> None:
    try:
        from dance_studio.core.config import BOT_TOKEN, BOOKINGS_ADMIN_CHAT_ID
    except Exception:
        return

    if not BOT_TOKEN or not BOOKINGS_ADMIN_CHAT_ID:
        return

    text = format_booking_message(booking, user)
    keyboard_data = build_booking_keyboard_data(booking.status, booking.object_type, booking.id)

    payload = {
        "chat_id": BOOKINGS_ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if keyboard_data:
        payload["reply_markup"] = {"inline_keyboard": keyboard_data}

    telegram_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(telegram_api_url, json=payload, timeout=5)
    except Exception:
        pass


@app.route("/api/booking-requests", methods=["GET"])
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
            return {"error": "date_from должен быть в формате YYYY-MM-DD"}, 400
    if date_to:
        try:
            date_to_val = datetime.strptime(date_to, "%Y-%m-%d").date()
            query = query.filter(BookingRequest.date <= date_to_val)
        except ValueError:
            return {"error": "date_to должен быть в формате YYYY-MM-DD"}, 400

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
            "teacher_id": booking.teacher_id,
            "date": booking.date.isoformat(),
            "time_from": time_from_str,
            "time_to": time_to_str,
            "status": booking.status,
            "status_label": BOOKING_STATUS_LABELS.get(booking.status, booking.status),
            "user_name": booking.user_name,
            "comment": booking.comment,
            "lessons_count": booking.lessons_count,
            "group_start_date": booking.group_start_date.isoformat() if booking.group_start_date else None,
            "valid_until": booking.valid_until.isoformat() if booking.valid_until else None,
        })

    return jsonify(result)


@app.route("/api/booking-requests", methods=["POST"])
def create_booking_request():
    db = g.db
    data = request.json or {}

    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    object_type = data.get("object_type")
    if object_type not in ["rental", "individual", "group"]:
        return {"error": "object_type должен быть rental, individual или group"}, 400

    teacher_id_val = None
    teacher = None
    if "teacher_id" in data:
        try:
            teacher_id_val = int(data.get("teacher_id"))
        except (TypeError, ValueError):
            return {"error": "teacher_id должен быть числом"}, 400
        teacher = db.query(Staff).filter_by(id=teacher_id_val, status="active").first()
        if not teacher:
            return {"error": "Преподаватель не найден"}, 404

    if object_type == "individual" and not teacher_id_val:
        return {"error": "teacher_id обязателен для индивидуального занятия"}, 400

    date_str = data.get("date")
    time_from_str = data.get("time_from")
    time_to_str = data.get("time_to")
    comment = data.get("comment")
    group_id = data.get("group_id")
    lessons_count = data.get("lessons_count")
    group_start_date_str = data.get("start_date")
    valid_until_str = data.get("valid_until")

    date_val = None
    time_from_val = None
    time_to_val = None
    group_id_val = None
    lessons_count_val = None
    group_start_date_val = None
    valid_until_val = None

    if object_type != "group":
        if not date_str or not time_from_str or not time_to_str:
            return {"error": "date, time_from и time_to обязательны"}, 400

    if date_str:
        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date должен быть в формате YYYY-MM-DD"}, 400
    if time_from_str:
        try:
            time_from_val = datetime.strptime(time_from_str, "%H:%M").time()
        except ValueError:
            return {"error": "time_from должен быть в формате HH:MM"}, 400
    if time_to_str:
        try:
            time_to_val = datetime.strptime(time_to_str, "%H:%M").time()
        except ValueError:
            return {"error": "time_to должен быть в формате HH:MM"}, 400

    if group_id is not None:
        try:
            group_id_val = int(group_id)
        except (TypeError, ValueError):
            return {"error": "group_id должен быть числом"}, 400

    if lessons_count is not None:
        try:
            lessons_count_val = int(lessons_count)
        except (TypeError, ValueError):
            return {"error": "lessons_count должен быть числом"}, 400
        if lessons_count_val <= 0:
            return {"error": "lessons_count должен быть больше 0"}, 400

    if group_start_date_str:
        try:
            group_start_date_val = datetime.strptime(group_start_date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "start_date должен быть в формате YYYY-MM-DD"}, 400

    if valid_until_str:
        try:
            valid_until_val = datetime.strptime(valid_until_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "valid_until должен быть в формате YYYY-MM-DD"}, 400

    if time_from_val and time_to_val and time_from_val >= time_to_val:
        return {"error": "time_from должен быть меньше time_to"}, 400

    overlaps = []
    if date_val and time_from_val and time_to_val:
        overlaps = _find_booking_overlaps(db, date_val, time_from_val, time_to_val)

    status = "AWAITING_PAYMENT" if object_type == "group" else "NEW"

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
        lessons_count=lessons_count_val,
        group_start_date=group_start_date_val,
        valid_until=valid_until_val,
    )

    db.add(booking)
    db.flush()

    individual_lesson = None
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
            status=status
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
            title="Индивидуальное занятие",
            start_time=time_from_val,
            end_time=time_to_val,
            teacher_id=teacher_id_val,
        )
        db.add(lesson_schedule)

    db.commit()

    _notify_booking_admins(booking, user)

    return {
        "id": booking.id,
        "status": booking.status,
        "overlaps": overlaps,
    }, 201


@app.route("/api/rental-occupancy")
def rental_occupancy():
    db = g.db
    date_str = request.args.get("date")
    if not date_str:
        date_val = datetime.now().date()
    else:
        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date должен быть в формате YYYY-MM-DD"}, 400

    entries = db.query(Schedule).filter(
        Schedule.object_type == "rental",
        Schedule.date == date_val,
        Schedule.time_from.isnot(None),
        Schedule.time_to.isnot(None),
        Schedule.status.notin_(["cancelled", "deleted"])
    ).all()

    result = []
    for entry in entries:
        result.append({
            "id": entry.id,
            "date": entry.date.isoformat() if entry.date else None,
            "time_from": entry.time_from.strftime("%H:%M") if entry.time_from else None,
            "time_to": entry.time_to.strftime("%H:%M") if entry.time_to else None,
            "status": entry.status,
            "title": entry.title or "Аренда"
        })

    return jsonify(result), 200


@app.route("/api/hall-occupancy")
def hall_occupancy():
    db = g.db
    date_str = request.args.get("date")
    if not date_str:
        date_val = datetime.now().date()
    else:
        try:
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "date должен быть в формате YYYY-MM-DD"}, 400

    entries = db.query(Schedule).filter(
        Schedule.date == date_val,
        Schedule.time_from.isnot(None),
        Schedule.time_to.isnot(None),
        Schedule.status.notin_(["cancelled", "deleted"])
    ).order_by(Schedule.time_from.asc()).all()

    result = []
    for entry in entries:
        result.append({
            "id": entry.id,
            "date": entry.date.isoformat() if entry.date else None,
            "time_from": entry.time_from.strftime("%H:%M") if entry.time_from else None,
            "time_to": entry.time_to.strftime("%H:%M") if entry.time_to else None,
            "status": entry.status,
            "title": entry.title or "Событие",
            "object_type": entry.object_type
        })

    app.logger.info("hall occupancy %s -> %s entries", date_val, len(result))
    return jsonify(result), 200


@app.route("/api/individual-lessons/<int:lesson_id>")
def get_individual_lesson(lesson_id):
    db = g.db
    lesson = db.query(IndividualLesson).filter_by(id=lesson_id).first()
    if not lesson:
        return {"error": "Индивидуальное занятие не найдено"}, 404

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
            "name": teacher.name if teacher else "—"
        },
        "student": {
            "id": student.id if student else None,
            "name": student.name if student else "—",
            "telegram_id": student.telegram_id if student else None,
            "username": student.username if student else None
        }
    })


def get_next_group_date(db, group_id):
    today = datetime.now().date()
    q = db.query(Schedule).filter(
        Schedule.object_type == "group",
        Schedule.date.isnot(None),
        Schedule.status != "cancelled",
        ((Schedule.group_id == group_id) | (Schedule.object_id == group_id)),
        Schedule.date >= today
    ).order_by(Schedule.date.asc())
    item = q.first()
    if item and item.date:
        return item.date
    return today


@app.route("/api/group-abonements/create", methods=["POST"])
def create_group_abonement():
    db = g.db
    data = request.json or {}

    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    group_id = data.get("group_id")
    lessons_count = data.get("lessons_count")
    if not group_id or not lessons_count:
        return {"error": "group_id и lessons_count обязательны"}, 400

    try:
        group_id = int(group_id)
        lessons_count = int(lessons_count)
    except (TypeError, ValueError):
        return {"error": "group_id и lessons_count должны быть числами"}, 400

    if lessons_count <= 0:
        return {"error": "lessons_count должен быть больше 0"}, 400

    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Группа не найдена"}, 404

    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first()
    price_per_lesson = direction.base_price if direction else None
    if not price_per_lesson or price_per_lesson <= 0:
        return {"error": "Цена направления не задана"}, 400

    total_amount = lessons_count * price_per_lesson
    base_date = get_next_group_date(db, group_id)
    valid_from = datetime.combine(base_date, time.min)
    valid_to = valid_from + timedelta(days=30)

    abonement = GroupAbonement(
        user_id=user.id,
        group_id=group_id,
        balance_credits=lessons_count,
        status="pending_activation",
        valid_from=valid_from,
        valid_to=valid_to
    )
    db.add(abonement)
    db.flush()

    payment = PaymentTransaction(
        user_id=user.id,
        amount=total_amount,
        currency="RUB",
        provider="stub",
        status="pending",
        description=f"Абонемент на {lessons_count} занятий",
        meta=json.dumps({"abonement_id": abonement.id})
    )
    db.add(payment)
    db.commit()

    return {
        "abonement_id": abonement.id,
        "payment_id": payment.id,
        "amount": total_amount,
        "currency": "RUB",
        "valid_from": abonement.valid_from.isoformat() if abonement.valid_from else None,
        "valid_to": abonement.valid_to.isoformat() if abonement.valid_to else None,
        "status": "pending"
    }, 201


@app.route("/api/payment-transactions/<int:payment_id>/pay", methods=["POST"])
def pay_transaction(payment_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    payment = db.query(PaymentTransaction).filter_by(id=payment_id, user_id=user.id).first()
    if not payment:
        return {"error": "Транзакция не найдена"}, 404

    if payment.status == "paid":
        return {"status": "already_paid"}

    payment.status = "paid"
    payment.paid_at = datetime.now()

    abonement = None
    if payment.meta:
        try:
            meta = json.loads(payment.meta)
            abonement_id = meta.get("abonement_id")
            if abonement_id:
                abonement = db.query(GroupAbonement).filter_by(id=abonement_id, user_id=user.id).first()
        except Exception:
            abonement = None

    if not abonement:
        abonement = db.query(GroupAbonement).filter_by(user_id=user.id, status="pending_activation").order_by(GroupAbonement.created_at.desc()).first()

    if abonement:
        abonement.status = "active"

    db.commit()
    return {"status": "paid"}


@app.route("/api/admin/group-abonements/<int:abonement_id>/activate", methods=["POST"])
def admin_activate_abonement(abonement_id):
    """
    Активация абонемента админом (например, после оплаты в Telegram).
    Меняет статус абонемента на active и, если есть связанная транзакция, ставит её в paid.
    """
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "Абонемент не найден"}, 404

    # Ищем связанную оплату
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


@app.route("/api/payment-transactions/my", methods=["GET"])
def get_my_transactions():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    items = db.query(PaymentTransaction).filter_by(user_id=user.id).order_by(PaymentTransaction.created_at.desc()).all()
    result = []
    for t in items:
        result.append({
            "id": t.id,
            "amount": t.amount,
            "currency": t.currency,
            "provider": t.provider,
            "status": t.status,
            "description": t.description,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "paid_at": t.paid_at.isoformat() if t.paid_at else None
        })

    return jsonify(result)


@app.route("/api/group-abonements/my", methods=["GET"])
def get_my_abonements():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

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
            "balance_credits": a.balance_credits,
            "status": a.status,
            "valid_from": a.valid_from.isoformat() if a.valid_from else None,
            "valid_to": a.valid_to.isoformat() if a.valid_to else None
        })

    return jsonify(result)


@app.route("/api/groups/my", methods=["GET"])
def get_my_groups():
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

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

