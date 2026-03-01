from __future__ import annotations

from flask import g

from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID
from dance_studio.core.permissions import has_permission
from dance_studio.db.models import Staff, User

def _get_current_staff(db):
    tid = getattr(g, "telegram_id", None)
    if not tid:
        return None
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return None
    return db.query(Staff).filter_by(telegram_id=tid, status="active").first()

def get_telegram_user(optional: bool = True):
    telegram_id = getattr(g, "telegram_id", None)
    if not telegram_id:
        return ({"error": "auth required"}, 401) if not optional else None
    return {"id": telegram_id}

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

def get_current_user_from_request(db):
    telegram_id = getattr(g, "telegram_id", None)
    if not telegram_id:
        return None
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return None
    return db.query(User).filter_by(telegram_id=telegram_id).first()

__all__ = [
    "_get_current_staff",
    "check_permission",
    "get_current_user_from_request",
    "get_telegram_user",
    "require_permission",
]
