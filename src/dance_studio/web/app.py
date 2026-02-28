from flask import Flask, jsonify, send_from_directory, request, g, make_response
from datetime import date, time, datetime, timedelta
import os
import json
import re
import hashlib
import secrets
from werkzeug.utils import secure_filename
import logging
import uuid
import requests
from pathlib import Path
from urllib.parse import urlparse
from sqlalchemy import or_
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

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
    Attendance,
    AttendanceIntention,
    AttendanceReminder,
    ScheduleOverrides,
    GroupAbonementActionLog,
    PaymentTransaction,
    PaymentProfile,
    AppSetting,
    AppSettingChange,
    BookingRequest,
    SessionRecord,
)
from dance_studio.core.media_manager import (
    save_user_photo,
    delete_user_photo,
    create_required_directories,
)
from dance_studio.core.permissions import has_permission
from dance_studio.core.tech_notifier import send_critical_sync
from dance_studio.core.booking_utils import (
    BOOKING_STATUS_LABELS,
    BOOKING_TYPE_LABELS,
    format_booking_message,
    build_booking_keyboard_data,
)
from dance_studio.core.tg_auth import validate_init_data
from dance_studio.core.tg_replay import store_used_init_data
from dance_studio.core.abonement_pricing import (
    ABONEMENT_TYPE_MULTI,
    ABONEMENT_TYPE_TRIAL,
    AbonementPricingError,
    get_next_group_date as pricing_get_next_group_date,
    parse_booking_bundle_group_ids,
    quote_group_booking,
    serialize_group_booking_quote,
)
from dance_studio.core.system_settings_service import (
    SettingValidationError,
    get_setting_value,
    list_setting_changes,
    list_setting_specs,
    list_settings,
    update_setting,
)
from dance_studio.core.config import (
    OWNER_IDS,
    TECH_ADMIN_ID,
    BOT_TOKEN,
    APP_SECRET_KEY,
    SESSION_TTL_DAYS,
    MAX_SESSIONS_PER_USER,
    ROTATE_IF_DAYS_LEFT,
    WEB_APP_URL,
    COOKIE_SECURE,
    COOKIE_SAMESITE,
    SESSION_PEPPER,
    CSRF_TRUSTED_ORIGINS,
    TG_INIT_DATA_MAX_AGE_SECONDS,
    SESSION_REAUTH_IDLE_SECONDS,
)

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
ATTENDANCE_ALLOWED_STATUSES = {"present", "absent", "late", "sick"}
ATTENDANCE_INTENTION_STATUS_WILL_MISS = "will_miss"
ATTENDANCE_INTENTION_LOCK_DELTA = timedelta(hours=2, minutes=30)
ATTENDANCE_INTENTION_LOCKED_MESSAGE = "Прием отметок закрыт. Напишите админу в случае чего-либо."
ATTENDANCE_MARKING_WINDOW_HOURS = 2
SESSION_TTL_SECONDS = SESSION_TTL_DAYS * 24 * 3600
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PATHS = {"/auth/telegram", "/auth/logout", "/health"}
CSRF_EXEMPT_PREFIXES = ("/api/directions/photo/",)

# Ensure media dirs exist at startup (var/media/*)
try:
    create_required_directories()
except Exception as e:
    logging.exception("Failed to create media directories on startup: %s", e)
SENSITIVE_PATH_PREFIXES = ("/schedule", "/api/bookings", "/api/payments", "/mailings", "/news")
INACTIVE_SCHEDULE_STATUSES = {
    "cancelled",
    "deleted",
    "rejected",
    "payment_failed",
    "CANCELLED",
    "DELETED",
    "REJECTED",
    "PAYMENT_FAILED",
}

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.logger.setLevel(logging.INFO)
app.secret_key = APP_SECRET_KEY
# Allow large photo uploads (up to 200 MB). Raise if bigger.
_MAX_UPLOAD_MB = 200
app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_MB * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = _MAX_UPLOAD_MB * 1024 * 1024

# File logger for debugging (UTF-8)
try:
    log_file = VAR_ROOT / "app.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh.setFormatter(formatter)
    app.logger.addHandler(fh)
except Exception as e:
    logging.exception("Failed to set up file logger: %s", e)


def _hash_user_agent(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()


def _extract_ip_prefix() -> str | None:
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "").strip()
    if not ip:
        return None
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    if ":" in ip:
        return ":".join(ip.split(":")[:4])
    return ip


def _is_sensitive_endpoint() -> bool:
    return request.path.startswith(SENSITIVE_PATH_PREFIXES)


def _extract_init_data_from_request() -> str | None:
    # Accept both legacy and new header names so the WebApp can send either.
    header_data = request.headers.get("X-TG-Init-Data", "").strip()
    if not header_data:
        header_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if header_data:
        return header_data

    auth_data = _get_init_data_from_auth_header()
    if auth_data:
        return auth_data

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        body_data = payload.get("init_data") or payload.get("initData")
        if isinstance(body_data, str) and body_data.strip():
            return body_data.strip()
    return None


def _create_session(db, telegram_id: int, sid: str, now: datetime, expires_at: datetime, user_agent_hash: str | None, ip_prefix: str | None) -> None:
    db.add(SessionRecord(
        id=secrets.token_hex(32),
        sid_hash=_sid_hash(sid),
        telegram_id=telegram_id,
        user_agent_hash=user_agent_hash,
        ip_prefix=ip_prefix,
        need_reauth=False,
        reauth_reason=None,
        created_at=now,
        last_seen=now,
        expires_at=expires_at,
    ))


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(f"{sid}:{SESSION_PEPPER}".encode("utf-8")).hexdigest()


def _origin_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_same_origin(value: str | None, allowed_origins: set[str]) -> bool:
    origin = _origin_from_url(value)
    return bool(origin and origin in allowed_origins)


def _normalize_origin(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip().rstrip("/")
    if not value:
        return None

    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"


def _build_csrf_trusted_origins() -> set[str]:
    trusted: set[str] = set()

    web_origin = _origin_from_url(WEB_APP_URL)
    if web_origin:
        trusted.add(web_origin)

    if request.scheme and request.host:
        trusted.add(f"{request.scheme}://{request.host}")

    for origin in CSRF_TRUSTED_ORIGINS.split(','):
        normalized = _normalize_origin(origin)
        if normalized:
            trusted.add(normalized)

    return trusted


def _build_image_url(path: str | None) -> str | None:
    """
    Нормализует сохранённый относительный путь (var/media/..., database/media/...)
    в HTTP URL, который обслуживает /media/<path:...>.
    """
    if not path:
        return None

    norm = path.replace("\\", "/").lstrip("/")
    if norm.startswith("var/media/"):
        return "/media/" + norm[len("var/media/"):]
    if norm.startswith("database/media/"):
        return "/media/" + norm[len("database/media/"):]
    if norm.startswith("media/"):
        return "/media/" + norm[len("media/"):]
    return "/" + norm


def _get_current_staff(db):
    tid = getattr(g, "telegram_id", None)
    if not tid:
        return None
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return None
    return db.query(Staff).filter_by(telegram_id=tid, status="active").first()


def _can_edit_schedule_attendance(db, schedule: Schedule) -> bool:
    # В dev окружении разрешаем для упрощения тестов
    from dance_studio.core.config import ENV
    if ENV == "dev":
        return True
    telegram_id = getattr(g, "telegram_id", None)
    if telegram_id and check_permission(telegram_id, "manage_schedule"):
        return True
    staff = _get_current_staff(db)
    if staff and schedule.teacher_id == staff.id:
        return True
    return False


def _is_csrf_valid() -> bool:
    trusted = _build_csrf_trusted_origins()
    if not trusted:
        return False

    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")

    for allowed in trusted:
        if origin.startswith(allowed) or referer.startswith(allowed):
            return True
    return False


def _delete_expired_sessions_for_user(db, telegram_id: int) -> None:
    db.query(SessionRecord).filter(
        SessionRecord.telegram_id == telegram_id,
        SessionRecord.expires_at < datetime.utcnow(),
    ).delete(synchronize_session=False)


def _enforce_session_limit(db, telegram_id: int) -> None:
    sessions = db.query(SessionRecord).filter(
        SessionRecord.telegram_id == telegram_id
    ).order_by(SessionRecord.created_at.desc()).all()
    stale = sessions[MAX_SESSIONS_PER_USER:]
    for rec in stale:
        db.delete(rec)


def _set_sid_cookie(response, sid: str) -> None:
    response.set_cookie(
        "sid",
        sid,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def _clear_sid_cookie(response) -> None:
    response.set_cookie(
        "sid",
        "",
        max_age=0,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def _get_init_data_from_auth_header() -> str | None:
    auth_header = request.headers.get("Authorization", "").strip()
    if not auth_header:
        return None
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return auth_header


@app.route("/auth/telegram", methods=["POST"])
def auth_telegram():
    db = g.db
    init_data = _extract_init_data_from_request()
    if not init_data:
        return {"error": "Authorization initData is required"}, 400

    verified = validate_init_data(init_data)
    if not verified:
        return {"error": "init_data недействителен"}, 401

    telegram_id = verified.user_id

    sid = secrets.token_hex(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(days=SESSION_TTL_DAYS)
    user_agent_hash = _hash_user_agent(request.headers.get("User-Agent"))
    ip_prefix = _extract_ip_prefix()

    try:
        replay_ttl = TG_INIT_DATA_MAX_AGE_SECONDS + 60
        if not store_used_init_data(db, verified.replay_key, replay_ttl):
            return {"error": "replay detected", "code": "replay_detected"}, 401

        _delete_expired_sessions_for_user(db, telegram_id)
        _create_session(db, telegram_id, sid, now, expires_at, user_agent_hash, ip_prefix)
        db.flush()
        _enforce_session_limit(db, telegram_id)
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Failed to create telegram auth session")
        return {"error": "Не удалось создать сессию"}, 500

    response = jsonify({"ok": True, "telegram_id": telegram_id})
    _set_sid_cookie(response, sid)
    return response


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    db = g.db
    sid = request.cookies.get("sid")
    if sid:
        try:
            db.query(SessionRecord).filter(SessionRecord.sid_hash == _sid_hash(sid)).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            app.logger.exception("Failed to logout session")
            return {"error": "Не удалось завершить сессию"}, 500

    response = jsonify({"ok": True})
    _clear_sid_cookie(response)
    return response


def get_telegram_user(optional: bool = True):
    telegram_id = getattr(g, "telegram_id", None)
    if not telegram_id:
        return ({"error": "auth required"}, 401) if not optional else None
    return {"id": telegram_id}

# ====== Проверка прав по telegram_id ======
def check_permission(telegram_id, permission):
    db = g.db
    staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    if not staff or not staff.position:
        return False
    staff_position = staff.position.strip().lower()
    return has_permission(staff_position, permission)


def require_permission(permission, allow_self_staff_id=None):
    telegram_id = getattr(g, "telegram_id", None)

    if not telegram_id:
        return {"error": "Требуется аутентификация"}, 401

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
    import traceback
    try:
        send_critical_sync(f"? Flask error: {type(error).__name__}: {error}")
    except (RuntimeError, ValueError, requests.RequestException):
        app.logger.exception("Failed to send critical error notification")

    payload = {
        "error": "Internal server error",
        "exception": f"{type(error).__name__}: {error}",
        "trace": traceback.format_exc(),
    }
    app.logger.error("Unhandled exception: %s\n%s", error, payload["trace"])
    return jsonify(payload), 500


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(error):
    max_mb = app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024) or _MAX_UPLOAD_MB
    app.logger.warning(
        "upload too large: content_length=%s max_mb=%s path=%s",
        request.content_length,
        max_mb,
        request.path,
    )
    return (
        jsonify({"error": "Файл слишком большой", "max_mb": max_mb}),
        413,
    )

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

class PaymentProfileModelView(ModelView):
    column_list = ['id', 'slot', 'recipient_bank', 'recipient_number', 'recipient_full_name', 'is_active', 'updated_at']
    column_filters = ['slot', 'is_active', 'updated_at']
    form_columns = ['slot', 'recipient_bank', 'recipient_number', 'recipient_full_name', 'is_active']

class AppSettingModelView(ModelView):
    column_list = ['id', 'key', 'value_type', 'is_public', 'updated_by_staff_id', 'updated_at']
    column_searchable_list = ['key', 'description']
    column_filters = ['value_type', 'is_public', 'updated_at']
    form_columns = ['key', 'value_json', 'value_type', 'description', 'is_public', 'updated_by_staff_id']

class AppSettingChangeModelView(ModelView):
    can_create = False
    can_edit = False
    can_delete = False
    column_list = ['id', 'setting_key', 'old_value_json', 'new_value_json', 'changed_by_staff_id', 'source', 'created_at']
    column_searchable_list = ['setting_key', 'change_reason']
    column_filters = ['setting_key', 'source', 'created_at']

admin.add_view(UserModelView(User, Session()))
admin.add_view(StaffModelView(Staff, Session()))
admin.add_view(NewsModelView(News, Session()))
admin.add_view(MailingModelView(Mailing, Session()))
admin.add_view(ScheduleModelView(Schedule, Session()))
admin.add_view(DirectionModelView(Direction, Session()))
admin.add_view(DirectionUploadSessionModelView(DirectionUploadSession, Session()))
admin.add_view(PaymentProfileModelView(PaymentProfile, Session()))
admin.add_view(AppSettingModelView(AppSetting, Session()))
admin.add_view(AppSettingChangeModelView(AppSettingChange, Session()))

# Автоматическое управление сессиями
@app.before_request
def before_request():
    g.db = get_session()
    g.telegram_user = None
    g.telegram_id = None
    g.rotate_sid = None
    g.clear_sid_cookie = False
    g.need_reauth = False

    if request.method in STATE_CHANGING_METHODS and request.path not in CSRF_EXEMPT_PATHS:
        if not any(request.path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            if not _is_csrf_valid():
                return {"error": "CSRF validation failed"}, 403

    sid = request.cookies.get("sid")
    if not sid:
        return

    try:
        db = g.db
        session = db.query(SessionRecord).filter_by(sid_hash=_sid_hash(sid)).first()
        if not session:
            g.clear_sid_cookie = True
            return

        now = datetime.utcnow()
        if session.expires_at <= now:
            db.delete(session)
            db.commit()
            g.clear_sid_cookie = True
            return

        ip_prefix = _extract_ip_prefix()
        should_commit = False

        if session.ip_prefix and ip_prefix and session.ip_prefix != ip_prefix:
            session.need_reauth = True
            session.reauth_reason = "ip_prefix_changed"
            should_commit = True

        if session.last_seen and (now - session.last_seen).total_seconds() > SESSION_REAUTH_IDLE_SECONDS:
            session.need_reauth = True
            session.reauth_reason = session.reauth_reason or "idle_timeout"
            should_commit = True

        if session.need_reauth and _is_sensitive_endpoint():
            init_data = _extract_init_data_from_request()
            if not init_data:
                return {"error": "need_reauth", "code": "need_reauth"}, 401

            verified = validate_init_data(init_data)
            if not verified or verified.user_id != session.telegram_id:
                return {"error": "need_reauth", "code": "need_reauth"}, 401

            replay_ttl = TG_INIT_DATA_MAX_AGE_SECONDS + 60
            if not store_used_init_data(db, verified.replay_key, replay_ttl):
                return {"error": "replay detected", "code": "replay_detected"}, 401

            new_sid = secrets.token_hex(32)
            new_expires_at = now + timedelta(days=SESSION_TTL_DAYS)
            _create_session(db, session.telegram_id, new_sid, now, new_expires_at, session.user_agent_hash, ip_prefix)
            db.delete(session)
            db.flush()
            _enforce_session_limit(db, session.telegram_id)
            g.rotate_sid = new_sid
            should_commit = True

            session = db.query(SessionRecord).filter_by(sid_hash=_sid_hash(new_sid)).first()

        telegram_id = session.telegram_id
        session.last_seen = now
        session.ip_prefix = ip_prefix or session.ip_prefix

        if session.expires_at - now < timedelta(days=ROTATE_IF_DAYS_LEFT):
            new_sid = secrets.token_hex(32)
            new_expires_at = now + timedelta(days=SESSION_TTL_DAYS)
            _create_session(db, session.telegram_id, new_sid, now, new_expires_at, session.user_agent_hash, session.ip_prefix)
            db.delete(session)
            db.flush()
            _enforce_session_limit(db, session.telegram_id)
            g.rotate_sid = new_sid
            should_commit = True
        else:
            should_commit = True

        if should_commit:
            db.commit()

        g.telegram_id = telegram_id
        g.telegram_user = {"id": telegram_id}
    except Exception:
        g.db.rollback()
        app.logger.exception("Session validation failed")
        g.clear_sid_cookie = True
        return


@app.teardown_request
def teardown_request(exception):
    db = getattr(g, 'db', None)
    if db is not None:
        db.close()


@app.after_request
def refresh_sid_cookie(response):
    if getattr(g, "clear_sid_cookie", False):
        _clear_sid_cookie(response)
    rotate_sid = getattr(g, "rotate_sid", None)
    if rotate_sid:
        _set_sid_cookie(response, rotate_sid)
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


@app.route("/assets/<path:filename>")
def serve_frontend_asset(filename):
    asset_path = Path(FRONTEND_DIR) / filename
    if asset_path.exists() and asset_path.is_file():
        return send_from_directory(FRONTEND_DIR, filename)
    return {"error": "file not found"}, 404


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/bot-username")
def get_bot_username():
    """Возвращает username бота для открытия чата."""
    db = g.db
    try:
        configured = get_setting_value(db, "contacts.bot_username")
        db.commit()
        if isinstance(configured, str) and configured.strip():
            return jsonify({"bot_username": configured.strip().lstrip("@")})
    except Exception:
        db.rollback()
        app.logger.exception("Failed to resolve bot username from system settings")

    try:
        from dance_studio.bot.bot import BOT_USERNAME_GLOBAL
        if BOT_USERNAME_GLOBAL:
            return jsonify({"bot_username": str(BOT_USERNAME_GLOBAL).strip().lstrip("@")})
    except Exception:
        app.logger.exception("Failed to resolve runtime bot username")

    return jsonify({"bot_username": "dance_studio_admin_bot"})


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
        today = date.today()
        mine_conditions = []

        # Индивидуальные занятия пользователя
        mine_conditions.append(
            (Schedule.object_type == "individual") & (IndividualLesson.student_id == user.id)
        )
        # Аренда, созданная пользователем
        mine_conditions.append(
            (Schedule.object_type == "rental") & (HallRental.creator_type == "user") & (HallRental.creator_id == user.id)
        )

        # Группы пользователя по активным абонементам
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

        # Если пользователь связан с сотрудником (преподаватель) — добавляем его группы
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
            return {"error": "Требуется авторизация"}, 401
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
    data = request.json or {}

    

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
            return {"error": "date должен быть в формате YYYY-MM-DD и быть существующей датой"}, 400
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
    
    schedule.status = "cancelled"
    schedule.status_comment = schedule.status_comment or "Отменено"
    db.commit()

    return {"ok": True, "message": "Занятие отменено"}


@app.route("/schedule/v2/<int:schedule_id>", methods=["DELETE"])


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


def _attendance_already_debited(db, attendance_id: int) -> bool:
    if not attendance_id:
        return False
    exists = db.query(GroupAbonementActionLog.id).filter_by(attendance_id=attendance_id).first()
    return bool(exists)


def _debit_abonement_for_attendance(db, attendance: Attendance, staff: Staff | None):
    if attendance.status == "sick":
        return False
    if _attendance_already_debited(db, attendance.id):
        return True
    if not attendance.abonement_id:
        return False
    abon = db.query(GroupAbonement).filter_by(id=attendance.abonement_id).first()
    if not abon or abon.balance_credits is None or abon.balance_credits <= 0:
        return False
    abon.balance_credits -= 1
    log = GroupAbonementActionLog(
        abonement_id=abon.id,
        action_type="debit_attendance",
        credits_delta=-1,
        attendance_id=attendance.id,
        actor_type="staff",
        actor_id=staff.id if staff else None,
    )
    db.add(log)
    return True


def _can_edit_schedule_attendance(db, schedule: Schedule) -> bool:
    window = _attendance_marking_window_info(schedule)
    return bool(window["is_open"])


def _load_group_roster(db, schedule: Schedule):
    if not schedule.group_id:
        return []
    date_val = schedule.date
    abonements = db.query(GroupAbonement).filter(
        GroupAbonement.group_id == schedule.group_id,
        GroupAbonement.status == "active",
    )
    if date_val:
        abonements = abonements.filter(
            or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= date_val),
            or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= date_val),
        )
    abonements = abonements.order_by(GroupAbonement.valid_to.is_(None), GroupAbonement.valid_to).all()
    roster = []
    seen = set()
    for abon in abonements:
        if abon.user_id in seen:
            continue
        seen.add(abon.user_id)
        user = db.query(User).filter_by(id=abon.user_id).first()
        if not user:
            continue
        roster.append({"user": user, "abonement": abon})
    return roster


def _schedule_group_id(schedule: Schedule) -> int | None:
    if schedule.group_id:
        return schedule.group_id
    if schedule.object_type == "group" and schedule.object_id:
        return schedule.object_id
    return None


def _can_user_set_absence_for_schedule(db, user: User, schedule: Schedule) -> bool:
    if schedule.status in {"cancelled", "deleted"}:
        return False

    if schedule.object_type == "group":
        group_id = _schedule_group_id(schedule)
        if not group_id:
            return False
        abon = _resolve_group_active_abonement(db, user.id, group_id, schedule.date)
        return bool(abon)

    if schedule.object_type == "individual":
        if not schedule.object_id:
            return False
        lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first()
        return bool(lesson and lesson.student_id == user.id)

    return False


def _schedule_start_datetime(schedule: Schedule) -> datetime | None:
    if not schedule.date:
        return None
    start_time = schedule.time_from or schedule.start_time
    if not start_time:
        return None
    return datetime.combine(schedule.date, start_time)


def _attendance_intention_lock_info(schedule: Schedule) -> dict:
    start_at = _schedule_start_datetime(schedule)
    if not start_at:
        return {
            "is_locked": False,
            "cutoff_at": None,
            "starts_at": None,
            "lock_message": None,
        }
    cutoff_at = start_at - ATTENDANCE_INTENTION_LOCK_DELTA
    is_locked = datetime.now() >= cutoff_at
    return {
        "is_locked": is_locked,
        "cutoff_at": cutoff_at.isoformat(),
        "starts_at": start_at.isoformat(),
        "lock_message": ATTENDANCE_INTENTION_LOCKED_MESSAGE if is_locked else None,
    }


def _attendance_marking_window_info(schedule: Schedule) -> dict:
    start_at = _schedule_start_datetime(schedule)
    if not start_at:
        return {
            "is_open": False,
            "phase": "unknown",
            "starts_at": None,
            "ends_at": None,
            "message": "Время занятия не задано.",
        }
    ends_at = start_at + timedelta(hours=ATTENDANCE_MARKING_WINDOW_HOURS)
    now = datetime.now()
    if now < start_at:
        return {
            "is_open": False,
            "phase": "before_start",
            "starts_at": start_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "message": "До начала занятия показывается предварительная отметка: кто придет и кто не придет.",
        }
    if now <= ends_at:
        return {
            "is_open": True,
            "phase": "marking_open",
            "starts_at": start_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "message": f"Можно отмечать фактическую посещаемость до {ends_at.strftime('%d.%m.%Y %H:%M')}.",
        }
    return {
        "is_open": False,
        "phase": "marking_closed",
        "starts_at": start_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "message": f"Окно отметки закрыто. Напишите админу в случае чего-либо.",
    }


@app.route("/api/attendance/<int:schedule_id>", methods=["GET"])
def get_attendance(schedule_id):
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    if not _can_edit_schedule_attendance(db, schedule):
        return {"error": "Нет доступа"}, 403

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
        "present": "Присутствовал",
        "absent": "Отсутствовал",
        "late": "Опоздал",
        "sick": "Болел",
    }

    return {
        "items": items,
        "source": roster_source or "manual",
        "status_labels": status_labels,
        "debit_policy": "Списывается 1 занятие для всех статусов, кроме 'sick'",
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


@app.route("/api/attendance/<int:schedule_id>", methods=["POST"])
def set_attendance(schedule_id):
    db = g.db
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    window = _attendance_marking_window_info(schedule)
    if not window["is_open"]:
        return {
            "error": "Окно отметки закрыто.",
            "attendance_phase": window["phase"],
            "attendance_phase_message": window["message"],
            "attendance_starts_at": window["starts_at"],
            "attendance_mark_until": window["ends_at"],
        }, 403

    data = request.json or {}
    items = data.get("items") or []
    if not isinstance(items, list):
        return {"error": "items должен быть списком"}, 400

    staff = _get_current_staff(db)
    results = []
    now = datetime.utcnow()

    for item in items:
        user_id = item.get("user_id")
        status = (item.get("status") or "").lower()
        comment = item.get("comment")
        if status not in ATTENDANCE_ALLOWED_STATUSES:
            return {"error": f"Недопустимый статус: {status}"}, 400
        if not user_id:
            return {"error": "user_id обязателен"}, 400
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return {"error": "user_id должен быть числом"}, 400

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


@app.route("/api/attendance/<int:schedule_id>/add-user", methods=["POST"])
def add_attendance_user(schedule_id):
    db = g.db
    if not has_permission(getattr(g, "telegram_id", None) or 0, "manage_schedule"):
        return {"error": "Нет доступа"}, 403
    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404
    window = _attendance_marking_window_info(schedule)
    if not window["is_open"]:
        return {
            "error": "Окно отметки закрыто.",
            "attendance_phase": window["phase"],
            "attendance_phase_message": window["message"],
            "attendance_starts_at": window["starts_at"],
            "attendance_mark_until": window["ends_at"],
        }, 403

    data = request.json or {}
    user_id = data.get("user_id")
    if not user_id:
        return {"error": "user_id обязателен"}, 400
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return {"error": "user_id должен быть числом"}, 400

    user = db.query(User).filter_by(id=user_id_int).first()
    if not user:
        return {"error": "Пользователь не найден"}, 404

    existing = db.query(Attendance).filter_by(schedule_id=schedule_id, user_id=user_id_int).first()
    if existing:
        return {"message": "Пользователь уже в списке"}, 200

    att = Attendance(
        schedule_id=schedule_id,
        user_id=user_id_int,
        status=data.get("status") or "absent",
        comment=data.get("comment"),
    )
    db.add(att)
    db.commit()
    return {"message": "Добавлено", "user_id": user_id_int}


def _serialize_attendance_intention(row: AttendanceIntention | None) -> dict:
    if not row:
        return {
            "has_intention": False,
            "status": None,
            "reason": None,
            "updated_at": None,
        }
    return {
        "has_intention": True,
        "status": row.status,
        "reason": row.reason,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_attendance_intention_with_lock(row: AttendanceIntention | None, lock_info: dict) -> dict:
    payload = _serialize_attendance_intention(row)
    payload.update(lock_info)
    if lock_info.get("is_locked"):
        payload["banner"] = ATTENDANCE_INTENTION_LOCKED_MESSAGE
    else:
        payload["banner"] = None
    return payload


@app.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["GET"])
def get_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "Нельзя отметиться для этого занятия"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()
    return _serialize_attendance_intention_with_lock(row, lock_info), 200


@app.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["POST"])
def set_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "Нельзя отметиться для этого занятия"}, 403

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


@app.route("/api/attendance-intentions/<int:schedule_id>/my", methods=["DELETE"])
def delete_my_attendance_intention(schedule_id):
    db = g.db
    user = get_current_user_from_request(db)
    if not user:
        return {"error": "Пользователь не найден"}, 401

    schedule = db.query(Schedule).filter_by(id=schedule_id).first()
    if not schedule:
        return {"error": "Занятие не найдено"}, 404

    if not _can_user_set_absence_for_schedule(db, user, schedule):
        return {"error": "Нельзя отметиться для этого занятия"}, 403

    lock_info = _attendance_intention_lock_info(schedule)
    if lock_info["is_locked"]:
        return {"error": ATTENDANCE_INTENTION_LOCKED_MESSAGE, "lock": lock_info}, 403

    row = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()
    if row:
        db.delete(row)
        db.commit()
    return _serialize_attendance_intention_with_lock(None, lock_info), 200


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
        photo_url = _build_image_url(n.photo_path)
        
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
        photo_url = _build_image_url(n.photo_path)
        
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
        
        # Формируем относительный путь от корня проекта
        photo_path = os.path.relpath(file_path, PROJECT_ROOT)
        news.photo_path = photo_path
        db.commit()
        
        return {
            "id": news.id,
            "photo_path": _build_image_url(news.photo_path),
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


@app.route("/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    
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


@app.route("/users/me", methods=["GET"])
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


@app.route("/users/<int:user_id>", methods=["PUT"])
def update_user(user_id):
    perm_error = require_permission("manage_staff")
    if perm_error:
        return perm_error
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    
    if not user:
        return {"error": "Пользователь не найден"}, 404
    
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


@app.route("/users/<int:user_id>/photo", methods=["POST"])
def upload_user_photo(user_id):
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "user not found"}, 404

    if not user.telegram_id:
        return {"error": "telegram_id is not set for this user"}, 400

    staff = db.query(Staff).filter_by(telegram_id=user.telegram_id, status="active").first()
    if not staff:
        return {"error": "upload is allowed only for active staff user"}, 403

    if "photo" not in request.files:
        return {"error": "photo file is required"}, 400

    file = request.files["photo"]
    if file.filename == "":
        return {"error": "filename is empty"}, 400

    allowed_extensions = {"jpg", "jpeg", "png", "gif"}
    if not ("." in file.filename and file.filename.rsplit(".", 1)[1].lower() in allowed_extensions):
        return {"error": "unsupported file extension"}, 400

    try:
        if user.photo_path:
            delete_user_photo(user.photo_path)

        file_data = file.read()
        filename = "profile." + file.filename.rsplit(".", 1)[1].lower()
        photo_path = save_user_photo(user.id, file_data, filename)
        if not photo_path:
            return {"error": "failed to save photo"}, 500

        user.photo_path = photo_path
        db.commit()

        return {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "photo_path": user.photo_path,
            "message": "photo uploaded",
        }, 201
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/users/<int:user_id>/photo", methods=["DELETE"])
def delete_user_photo_endpoint(user_id):
    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "user not found"}, 404

    if not user.telegram_id:
        return {"error": "telegram_id is not set for this user"}, 400

    staff = db.query(Staff).filter_by(telegram_id=user.telegram_id, status="active").first()
    if not staff:
        return {"error": "delete is allowed only for active staff user"}, 403

    if not user.photo_path:
        return {"error": "photo not found"}, 404

    try:
        delete_user_photo(user.photo_path)
        user.photo_path = None
        db.commit()
        return {"ok": True, "message": "photo deleted"}
    except Exception as e:
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


@app.route("/user/<int:user_id>/photo")
def get_user_photo(user_id):
    """
    Получить фото, загруженное пользователем через бота
    """
    try:
        db = g.db
        user = db.query(User).filter_by(id=user_id).first()
        
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
    valid_positions = ["учитель", "администратор", "старший админ", "владелец", "тех. админ"]
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
                            "старший админ": "🛡️ Старший админ",
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
                "старший админ": "🛡️ Старший админ",
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
        "message": "мя обновлено из Telegram"
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
        valid_positions = {"учитель", "администратор", "старший админ", "модератор", "владелец", "тех. админ"}
        normalized_position = str(data["position"]).strip().lower()
        if normalized_position not in valid_positions:
            return {"error": f"Допустимые должности: {', '.join(valid_positions)}"}, 400
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
            return {"error": "Неверный telegram_id"}, 400
        actor_staff = None
        if actor_telegram_id is not None:
            actor_staff = db.query(Staff).filter_by(telegram_id=actor_telegram_id, status="active").first()
        allowed_positions = {"администратор", "старший админ", "владелец", "тех. админ"}
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


@app.route("/api/stats/teacher", methods=["GET"])
def get_teacher_stats():
    perm_error = require_permission("view_stats")
    if perm_error:
        return perm_error

    db = g.db
    try:
        teacher_id = int(request.args.get("teacher_id", 0))
    except (TypeError, ValueError):
        return {"error": "teacher_id должен быть числом"}, 400
    if not teacher_id:
        return {"error": "teacher_id обязателен"}, 400

    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    try:
        date_from_val = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        date_to_val = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
    except ValueError:
        return {"error": "Неверный формат даты, используйте YYYY-MM-DD"}, 400

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
                f" К сожалению...\n\n"
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
        
        # щем среди пользователей (Users), а не среди персонала (Staff)
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


# ======================== ССТЕМА РАССЫЛОК ========================

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
    """нициирует отправку рассылки"""
    perm_error = require_permission("manage_mailings")
    if perm_error:
        return perm_error

    try:
        # мпортируем функцию добавления рассылки в очередь
        from dance_studio.bot.bot import queue_mailing_for_sending
        
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


# ======================== ССТЕМА УПРАВЛЕНЯ НАПРАВЛЕНЯМ ========================

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


@app.route("/api/directions/<int:direction_id>", methods=["GET"])
def get_direction(direction_id):
    """Возвращает одно направление по ID для формы редактирования"""
    db = g.db
    direction = db.query(Direction).filter_by(direction_id=direction_id).first()
    if not direction:
        return {"error": "Направление не найдено"}, 404

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


@app.route("/api/groups/compatible", methods=["GET"])
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
        if v in ("1", "true", "yes", "y", "Р Т‘Р В°"):
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

        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        storage_id = user.id if user else telegram_id
        photo_path = save_user_photo(storage_id, photo_resp.content)
        if not photo_path:
            return

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
    
    telegram_user_id = getattr(g, "telegram_id", None)
    if not telegram_user_id:
        return {"error": "Требуется авторизация"}, 401

    try:
        telegram_user_id = int(telegram_user_id)
    except (TypeError, ValueError):
        return {"error": "Неверный telegram_id"}, 400

    admin = db.query(Staff).filter_by(telegram_id=telegram_user_id).first()
    if not admin or admin.position not in ["администратор", "старший админ", "владелец", "тех. админ"]:
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
    try:
        db = g.db

        session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
        if not session:
            app.logger.warning("direction upload status: session not found token=%s", token)
            return {"error": "Сессия не найдена"}, 404

        app.logger.info(
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
        app.logger.error("upload-complete error: %s\n%s", exc, trace)
        return {"error": "internal", "exception": str(exc), "trace": trace}, 500


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
        return {"error": "session_token обязателен"}, 400

    session = db.query(DirectionUploadSession).filter_by(session_token=session_token).first()
    if not session:
        print(f"[create_direction] session not found: {session_token}")
        return {"error": "Сессия не найдена"}, 404

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
        "message": "Направление успешно создано"
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
    спользуется ботом при получении фотографии от администратора
    """
    db = g.db

    app.logger.info("direction photo upload start token=%s", token)

    session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
    if not session:
        app.logger.warning("direction upload: session not found token=%s", token)
        return {"error": "Сессия не найдена"}, 404

    if "photo" not in request.files:
        app.logger.warning("direction upload: no file provided token=%s", token)
        return {"error": "Файл не загружен"}, 400

    file = request.files["photo"]
    if file.filename == "":
        app.logger.warning("direction upload: empty filename token=%s", token)
        return {"error": "Файл не выбран"}, 400

    try:
        # Сохраняем в var/media/directions/<session_id>/photo_xxx.ext
        directions_dir = MEDIA_ROOT / "directions" / str(session.session_id)
        os.makedirs(directions_dir, exist_ok=True)

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
        if not ext:
            return {"error": "Не удалось определить тип файла"}, 400
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in {".jpg", ".png", ".webp"}:
            return {"error": "Поддерживаются только JPG/PNG/WEBP"}, 400

        filename = secure_filename(f"photo_{session.session_id}{ext}")
        filepath = directions_dir / filename
        file.save(filepath)

        # Сохраняем путь в БД относительно корня проекта
        relative_path = os.path.relpath(filepath, PROJECT_ROOT)
        session.image_path = relative_path
        session.status = "photo_received"
        db.commit()

        app.logger.info(
            "direction upload success session_id=%s path=%s",
            session.session_id,
            filepath,
        )

        return {
            "message": "Фотография загружена",
            "session_id": session.session_id,
            "status": "photo_received",
            "image_path": _build_image_url(session.image_path),
        }, 200

    except Exception as exc:
        db.rollback()
        app.logger.exception("Ошибка при загрузке фотографии направления: %s", exc)
        return {"error": f"Internal server error while saving photo: {exc}"}, 500


# ======================== ГРУППОВЫЕ АБОНЕМЕНТЫ / ОПЛАТЫ (ЗАГЛУШКА) ========================
PAYMENT_PROFILE_SLOTS = (1, 2)


def get_current_user_from_request(db):
    telegram_id = getattr(g, "telegram_id", None)
    if not telegram_id:
        return None
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return None
    return db.query(User).filter_by(telegram_id=telegram_id).first()


def _ensure_payment_profiles(db):
    profiles = (
        db.query(PaymentProfile)
        .filter(PaymentProfile.slot.in_(PAYMENT_PROFILE_SLOTS))
        .order_by(PaymentProfile.slot.asc())
        .all()
    )
    by_slot = {int(p.slot): p for p in profiles}
    created = False

    for slot in PAYMENT_PROFILE_SLOTS:
        if slot not in by_slot:
            profile = PaymentProfile(
                slot=slot,
                title="Основные реквизиты" if slot == 1 else "Резервные реквизиты",
                details="",
                recipient_bank="",
                recipient_number="",
                recipient_full_name="",
                is_active=(slot == 1),
            )
            db.add(profile)
            by_slot[slot] = profile
            created = True

    if created:
        db.flush()

    active_profiles = [p for p in by_slot.values() if p.is_active]
    if not active_profiles:
        by_slot[1].is_active = True
    elif len(active_profiles) > 1:
        for p in active_profiles:
            p.is_active = (p.slot == 1)

    return by_slot


def _serialize_payment_profile(profile: PaymentProfile) -> dict:
    recipient_bank = (profile.recipient_bank or "").strip()
    recipient_number = (profile.recipient_number or "").strip()
    recipient_full_name = (profile.recipient_full_name or "").strip()
    details = (
        f"Банк получателя: {recipient_bank or '—'}\n"
        f"Номер: {recipient_number or '—'}\n"
        f"ФИО получателя: {recipient_full_name or '—'}"
    )
    return {
        "slot": int(profile.slot),
        "title": profile.title or "",
        "details": details,
        "recipient_bank": recipient_bank,
        "recipient_number": recipient_number,
        "recipient_full_name": recipient_full_name,
        "is_active": bool(profile.is_active),
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def _get_active_payment_profile_payload(db) -> dict | None:
    active = (
        db.query(PaymentProfile)
        .filter(PaymentProfile.slot.in_(PAYMENT_PROFILE_SLOTS), PaymentProfile.is_active.is_(True))
        .order_by(PaymentProfile.slot.asc())
        .first()
    )
    if not active:
        active = (
            db.query(PaymentProfile)
            .filter(PaymentProfile.slot.in_(PAYMENT_PROFILE_SLOTS))
            .order_by(PaymentProfile.slot.asc())
            .first()
        )
    if not active:
        return None
    payload = _serialize_payment_profile(active)
    payload["label"] = "Профиль 1" if active.slot == 1 else "Профиль 2"
    return payload


@app.route("/api/payment-profiles/active", methods=["GET"])
def get_active_payment_profile():
    db = g.db
    profile = _get_active_payment_profile_payload(db)
    if not profile:
        return {"error": "Активные реквизиты оплаты не настроены"}, 404
    return jsonify(profile)


@app.route("/api/admin/payment-profiles", methods=["GET"])
def admin_get_payment_profiles():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    db = g.db
    profiles = _ensure_payment_profiles(db)
    db.commit()
    result = [_serialize_payment_profile(profiles[slot]) for slot in PAYMENT_PROFILE_SLOTS]
    active_slot = next((item["slot"] for item in result if item["is_active"]), 1)
    return jsonify({"profiles": result, "active_slot": active_slot})


@app.route("/api/admin/payment-profiles/<int:slot>", methods=["PUT"])
def admin_update_payment_profile(slot):
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    if slot not in PAYMENT_PROFILE_SLOTS:
        return {"error": "slot должен быть 1 или 2"}, 400

    db = g.db
    data = request.json or {}
    recipient_bank = str(data.get("recipient_bank") or "").strip()
    recipient_number = str(data.get("recipient_number") or "").strip()
    recipient_full_name = str(data.get("recipient_full_name") or "").strip()

    if not recipient_bank:
        return {"error": "recipient_bank обязателен"}, 400
    if not recipient_number:
        return {"error": "recipient_number обязателен"}, 400
    if not recipient_full_name:
        return {"error": "recipient_full_name обязателен"}, 400
    if len(recipient_bank) > 160:
        return {"error": "recipient_bank слишком длинный (максимум 160 символов)"}, 400
    if len(recipient_number) > 64:
        return {"error": "recipient_number слишком длинный (максимум 64 символа)"}, 400
    if len(recipient_full_name) > 160:
        return {"error": "recipient_full_name слишком длинный (максимум 160 символов)"}, 400

    profiles = _ensure_payment_profiles(db)
    profile = profiles[slot]
    profile.title = "Профиль 1" if slot == 1 else "Профиль 2"
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


@app.route("/api/admin/payment-profiles/active", methods=["PUT"])
def admin_switch_active_payment_profile():
    perm_error = require_permission("manage_schedule")
    if perm_error:
        return perm_error

    data = request.json or {}
    try:
        active_slot = int(data.get("active_slot"))
    except (TypeError, ValueError):
        return {"error": "active_slot должен быть числом 1 или 2"}, 400

    if active_slot not in PAYMENT_PROFILE_SLOTS:
        return {"error": "active_slot должен быть 1 или 2"}, 400

    db = g.db
    profiles = _ensure_payment_profiles(db)
    for slot, profile in profiles.items():
        profile.is_active = (slot == active_slot)
    db.commit()
    return jsonify({"active_slot": active_slot})


@app.route("/api/system-settings/public", methods=["GET"])
def get_public_system_settings():
    db = g.db
    items = list_settings(db, public_only=True)
    db.commit()
    return jsonify({"items": items, "specs": list_setting_specs(public_only=True)})


@app.route("/api/admin/system-settings", methods=["GET"])
def admin_get_system_settings():
    perm_error = require_permission("system_settings")
    if perm_error:
        return perm_error

    db = g.db
    items = list_settings(db, public_only=False)
    db.commit()
    return jsonify({"items": items, "specs": list_setting_specs(public_only=False)})


@app.route("/api/admin/system-settings/<path:key>", methods=["PUT"])
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


@app.route("/api/admin/system-settings/changes", methods=["GET"])
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
        Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES))
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
                "title": "ндивидуальное занятие"
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
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
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
    is_free_group_trial = (
        booking.object_type == "group"
        and (booking.abonement_type or "").strip().lower() == ABONEMENT_TYPE_TRIAL
        and int(booking.requested_amount or 0) == 0
    )
    keyboard_data = build_booking_keyboard_data(
        booking.status,
        booking.object_type,
        booking.id,
        is_free_group_trial=is_free_group_trial,
    )

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


def _compute_group_booking_payment_amount(db, booking: BookingRequest) -> int | None:
    if booking.object_type != "group":
        return None
    if booking.requested_amount is not None:
        try:
            amount = int(booking.requested_amount)
        except (TypeError, ValueError):
            return None
        return amount if amount >= 0 else None

    if not booking.group_id:
        return None
    try:
        quote = quote_group_booking(
            db,
            user_id=None,  # quote for already created booking should not be blocked by trial checks
            group_id=booking.group_id,
            abonement_type=booking.abonement_type or ABONEMENT_TYPE_MULTI,
            bundle_group_ids=parse_booking_bundle_group_ids(booking),
        )
    except AbonementPricingError:
        return None
    return quote.amount


def _build_booking_payment_request_message(db, booking: BookingRequest) -> str:
    profile = _get_active_payment_profile_payload(db) or {}
    bank = str(profile.get("recipient_bank") or "—").strip() or "—"
    number = str(profile.get("recipient_number") or "—").strip() or "—"
    full_name = str(profile.get("recipient_full_name") or "—").strip() or "—"

    amount = _compute_group_booking_payment_amount(db, booking)
    amount_text = f"{amount:,} ₽".replace(",", " ") if amount else "уточните у администратора"

    return (
        "Здравствуйте!\n"
        "Это администрация Shebba Sports x Lissa Dance Studio.\n\n"
        "Реквизиты для оплаты:\n"
        f"• Банк получателя: {bank}\n"
        f"• Номер: {number}\n"
        f"• ФИО получателя: {full_name}\n"
        f"• Сумма к оплате: {amount_text}\n\n"
        "Пожалуйста, после оплаты отправьте чек для подтверждения."
    )


def _humanize_userbot_error(raw_reason: str) -> str:
    reason = str(raw_reason or "").strip()
    if not reason:
        return "неизвестная ошибка"

    # Unwrap wrappers like "userbot returned: {...}" and keep the most specific error text.
    wrapped_match = re.search(r"userbot returned:\s*(.+)$", reason, flags=re.IGNORECASE)
    if wrapped_match:
        reason = wrapped_match.group(1).strip()

    dict_error_match = re.search(r"'error'\s*:\s*'([^']+)'", reason)
    if not dict_error_match:
        dict_error_match = re.search(r'"error"\s*:\s*"([^"]+)"', reason)
    if dict_error_match:
        reason = dict_error_match.group(1).strip()

    if reason in {"None", "null", "{}"}:
        return "userbot не вернул текст ошибки"

    # Specific Telethon/Telegram RPC code translations.
    allow_payment_match = re.search(r"\bALLOW_PAYMENT_REQUIRED_(\d+)\b", reason, flags=re.IGNORECASE)
    if allow_payment_match:
        stars = allow_payment_match.group(1)
        return f"Требуется {stars} звёзд Telegram для отправки сообщения (ALLOW_PAYMENT_REQUIRED_{stars})"

    known_codes = {
        "USER_IS_BLOCKED": "Пользователь запретил личные сообщения от аккаунта userbot",
        "CHAT_WRITE_FORBIDDEN": "Нет прав на отправку сообщения этому пользователю",
        "PEER_FLOOD": "Ограничение Telegram на частые действия (flood control)",
        "FLOOD_WAIT": "Telegram временно ограничил отправку сообщений (flood wait)",
        "PRIVACY_RESTRICTED": "Ограничения приватности пользователя не позволяют написать ему",
    }
    upper_reason = reason.upper()
    for code, text in known_codes.items():
        if code in upper_reason:
            return f"{text} ({code})"

    return reason


def _send_booking_payment_details_via_userbot(db, booking: BookingRequest, user: User | None) -> None:
    telegram_id = user.telegram_id if user else booking.user_telegram_id
    if not telegram_id:
        app.logger.warning("booking %s: skip payment DM, telegram_id missing", booking.id)
        return

    try:
        from dance_studio.bot.telegram_userbot import send_private_message_sync
    except Exception:
        app.logger.exception("booking %s: userbot import failed", booking.id)
        return

    payment_text = _build_booking_payment_request_message(db, booking)
    user_target = {
        "id": telegram_id,
        "username": user.username if user else booking.user_username,
        "phone": user.phone if user else None,
        "name": user.name if user else booking.user_name,
    }
    try:
        result = send_private_message_sync(user_target, payment_text)
        if not result:
            raise RuntimeError("userbot returned: None")
        if not result.get("ok"):
            detail = str(result.get("error") or "").strip()
            if detail:
                raise RuntimeError(detail)
            raise RuntimeError(f"userbot returned: {result!r}")
    except Exception as exc:
        app.logger.exception("booking %s: failed to deliver payment details via userbot", booking.id)
        try:
            from dance_studio.core.config import BOT_TOKEN, BOOKINGS_ADMIN_CHAT_ID

            if BOT_TOKEN and BOOKINGS_ADMIN_CHAT_ID:
                username = f"@{user_target['username']}" if user_target.get("username") else "—"
                reason = _humanize_userbot_error(str(exc))
                alert_text = (
                    "⚠️ Не удалось отправить реквизиты через userbot.\n"
                    f"Заявка: #{booking.id}\n"
                    f"Получатель: {user_target.get('name') or 'пользователь'} "
                    f"(id={telegram_id}, username={username})\n"
                    f"Причина: {reason}"
                )
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": BOOKINGS_ADMIN_CHAT_ID, "text": alert_text},
                    timeout=5,
                )
        except Exception:
            pass


@app.route("/api/booking-requests/group/quote", methods=["POST"])
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


@app.route("/api/booking-requests/my", methods=["GET"])
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
            bundle_group_names.append(group.name if group and group.name else f"Группа #{group_id}")

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


@app.route("/api/booking-requests", methods=["POST"])
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
            title="Аренда зала",
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
            title="Индивидуальное занятие",
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
            return {"error": "date должен быть в формате YYYY-MM-DD и быть существующей датой"}, 400

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
            return {"error": "date должен быть в формате YYYY-MM-DD и быть существующей датой"}, 400

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
        return {"error": "ндивидуальное занятие не найдено"}, 404

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
    return pricing_get_next_group_date(db, int(group_id))


@app.route("/api/groups/<int:group_id>/next-session", methods=["GET"])
def get_group_next_session(group_id: int):
    db = g.db
    group = db.query(Group).filter_by(id=group_id).first()
    if not group:
        return {"error": "Group not found"}, 404
    next_date = get_next_group_date(db, group_id)
    return jsonify({"group_id": group_id, "next_session_date": next_date.isoformat() if next_date else None})


@app.route("/api/group-abonements/create", methods=["POST"])
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

    # щем связанную оплату
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


def _parse_iso_date(value, field_name: str):
    if not value or not isinstance(value, str):
        raise ValueError(f"{field_name} обязателен и должен быть строкой формата YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} должен быть в формате YYYY-MM-DD") from exc


def _parse_user_id_for_merge(payload: dict, field_name: str) -> int:
    raw_value = payload.get(field_name)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def _merge_attendance_rows(db, source_user_id: int, target_user_id: int) -> dict:
    moved = 0
    merged = 0
    relinked_logs = 0

    rows = (
        db.query(Attendance)
        .filter(Attendance.user_id == source_user_id)
        .order_by(Attendance.id.asc())
        .all()
    )
    for source_row in rows:
        target_row = db.query(Attendance).filter(
            Attendance.schedule_id == source_row.schedule_id,
            Attendance.user_id == target_user_id,
        ).first()

        if not target_row:
            source_row.user_id = target_user_id
            moved += 1
            continue

        if source_row.marked_at and (not target_row.marked_at or source_row.marked_at > target_row.marked_at):
            target_row.marked_at = source_row.marked_at
            target_row.marked_by_staff_id = source_row.marked_by_staff_id

        if source_row.status and target_row.status != source_row.status:
            if target_row.status not in {"present", "late"} or source_row.status in {"present", "late"}:
                target_row.status = source_row.status

        if not target_row.abonement_id and source_row.abonement_id:
            target_row.abonement_id = source_row.abonement_id
        if not target_row.comment and source_row.comment:
            target_row.comment = source_row.comment

        relinked = (
            db.query(GroupAbonementActionLog)
            .filter(GroupAbonementActionLog.attendance_id == source_row.id)
            .update({GroupAbonementActionLog.attendance_id: target_row.id}, synchronize_session=False)
        )
        relinked_logs += int(relinked or 0)

        db.delete(source_row)
        merged += 1

    return {"moved": moved, "merged": merged, "relinked_logs": relinked_logs}


def _merge_attendance_intentions_rows(db, source_user_id: int, target_user_id: int) -> dict:
    moved = 0
    merged = 0

    rows = (
        db.query(AttendanceIntention)
        .filter(AttendanceIntention.user_id == source_user_id)
        .order_by(AttendanceIntention.id.asc())
        .all()
    )
    for source_row in rows:
        target_row = db.query(AttendanceIntention).filter(
            AttendanceIntention.schedule_id == source_row.schedule_id,
            AttendanceIntention.user_id == target_user_id,
        ).first()
        if not target_row:
            source_row.user_id = target_user_id
            moved += 1
            continue

        source_updated = source_row.updated_at or source_row.created_at
        target_updated = target_row.updated_at or target_row.created_at
        if source_updated and (not target_updated or source_updated > target_updated):
            target_row.status = source_row.status
            target_row.reason = source_row.reason
            target_row.source = source_row.source
            target_row.updated_at = source_row.updated_at
        elif not target_row.reason and source_row.reason:
            target_row.reason = source_row.reason

        db.delete(source_row)
        merged += 1

    return {"moved": moved, "merged": merged}


def _merge_attendance_reminders_rows(db, source_user_id: int, target_user_id: int) -> dict:
    moved = 0
    merged = 0

    rows = (
        db.query(AttendanceReminder)
        .filter(AttendanceReminder.user_id == source_user_id)
        .order_by(AttendanceReminder.id.asc())
        .all()
    )
    for source_row in rows:
        target_row = db.query(AttendanceReminder).filter(
            AttendanceReminder.schedule_id == source_row.schedule_id,
            AttendanceReminder.user_id == target_user_id,
        ).first()
        if not target_row:
            source_row.user_id = target_user_id
            moved += 1
            continue

        source_updated = source_row.updated_at or source_row.created_at
        target_updated = target_row.updated_at or target_row.created_at
        if source_updated and (not target_updated or source_updated > target_updated):
            target_row.send_status = source_row.send_status
            target_row.send_error = source_row.send_error
            target_row.attempted_at = source_row.attempted_at
            target_row.sent_at = source_row.sent_at
            target_row.telegram_chat_id = source_row.telegram_chat_id
            target_row.telegram_message_id = source_row.telegram_message_id
            target_row.responded_at = source_row.responded_at
            target_row.response_action = source_row.response_action
            target_row.button_closed_at = source_row.button_closed_at
            target_row.updated_at = source_row.updated_at
        elif not target_row.response_action and source_row.response_action:
            target_row.response_action = source_row.response_action
            target_row.responded_at = source_row.responded_at or target_row.responded_at

        db.delete(source_row)
        merged += 1

    return {"moved": moved, "merged": merged}


def _append_merge_note(current_value: str | None, note: str) -> str:
    existing = (current_value or "").strip()
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}\n\n{note}"


@app.route("/api/admin/clients/merge", methods=["POST"])
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


@app.route("/api/admin/clients/<int:user_id>/sick-leave", methods=["POST"])
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
        return {"error": "date_to не может быть раньше date_from"}, 400

    note = (payload.get("note") or "").strip()
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404

    staff = _get_current_staff(db)
    now = datetime.utcnow()
    range_key = f"{date_from.isoformat()}:{date_to.isoformat()}"
    sick_default_comment = f"Болел: {date_from.isoformat()} - {date_to.isoformat()}"
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
                note=f"Возврат занятия за больничный ({range_key})",
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
                note=f"Продление абонемента на {extension_days} дн. (больничный)",
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


def _serialize_client_abonement_for_admin(db, abonement: GroupAbonement) -> dict:
    group = db.query(Group).filter_by(id=abonement.group_id).first()
    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first() if group else None
    lessons_per_week = int(group.lessons_per_week) if group and group.lessons_per_week else None
    return {
        "id": abonement.id,
        "group_id": abonement.group_id,
        "group_name": group.name if group else None,
        "direction_title": direction.title if direction else None,
        "lessons_per_week": lessons_per_week,
        "abonement_type": abonement.abonement_type,
        "bundle_id": abonement.bundle_id,
        "bundle_size": abonement.bundle_size,
        "balance_credits": abonement.balance_credits,
        "status": abonement.status,
        "valid_from": abonement.valid_from.isoformat() if abonement.valid_from else None,
        "valid_to": abonement.valid_to.isoformat() if abonement.valid_to else None,
    }


def _parse_month_start(value: str | None):
    if not value:
        now = datetime.now()
        return date(now.year, now.month, 1)
    try:
        dt = datetime.strptime(value, "%Y-%m")
        return date(dt.year, dt.month, 1)
    except ValueError as exc:
        raise ValueError("month должен быть в формате YYYY-MM") from exc


@app.route("/api/admin/clients/<int:user_id>/abonements", methods=["GET"])
def admin_get_client_abonements(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404

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


@app.route("/api/admin/clients/<int:user_id>/attendance-calendar", methods=["GET"])
def admin_get_client_attendance_calendar(user_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        return {"error": "Клиент не найден"}, 404

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
            mark_code = "П"
            mark_label = "Пришел"
        elif status == "absent":
            mark_code = "Н"
            mark_label = "Неявка"
        elif status == "sick":
            mark_code = "Б"
            mark_label = "Больничный"
        elif status == "planned":
            mark_code = None
            mark_label = "Записан"

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
                "П": "Пришел",
                "Н": "Неявка",
                "Б": "Больничный",
            },
        }
    )


@app.route("/api/admin/group-abonements/<int:abonement_id>/extend", methods=["POST"])
def admin_extend_group_abonement(abonement_id: int):
    perm_error = require_permission("verify_certificate")
    if perm_error:
        return perm_error

    db = g.db
    payload = request.json or {}
    abonement = db.query(GroupAbonement).filter_by(id=abonement_id).first()
    if not abonement:
        return {"error": "Абонемент не найден"}, 404

    group = db.query(Group).filter_by(id=abonement.group_id).first()
    lessons_per_week = int(group.lessons_per_week) if group and group.lessons_per_week else None
    if not lessons_per_week or lessons_per_week <= 0:
        return {"error": "Для группы не настроено количество занятий в неделю"}, 400

    weeks_raw = payload.get("weeks")
    lessons_raw = payload.get("lessons")
    if weeks_raw in (None, "") and lessons_raw in (None, ""):
        return {"error": "Укажите weeks или lessons"}, 400

    weeks = None
    lessons = None
    if weeks_raw not in (None, ""):
        try:
            weeks = int(weeks_raw)
        except (TypeError, ValueError):
            return {"error": "weeks должен быть целым числом"}, 400
        if weeks <= 0:
            return {"error": "weeks должен быть больше 0"}, 400
    if lessons_raw not in (None, ""):
        try:
            lessons = int(lessons_raw)
        except (TypeError, ValueError):
            return {"error": "lessons должен быть целым числом"}, 400
        if lessons <= 0:
            return {"error": "lessons должен быть больше 0"}, 400

    if weeks is None and lessons is not None:
        if lessons % lessons_per_week != 0:
            return {"error": f"lessons должен быть кратен {lessons_per_week}"}, 400
        weeks = lessons // lessons_per_week
    elif lessons is None and weeks is not None:
        lessons = weeks * lessons_per_week
    else:
        expected_lessons = weeks * lessons_per_week
        if lessons != expected_lessons:
            return {"error": f"Несоответствие: при {weeks} нед. должно быть {expected_lessons} занятий"}, 400

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
            note=note or f"Продление абонемента: +{weeks} нед. / +{lessons} занятий",
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
            "abonement_type": a.abonement_type,
            "bundle_id": a.bundle_id,
            "bundle_size": a.bundle_size,
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

