from __future__ import annotations

from flask import g

from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID
from dance_studio.core.permissions import has_permission
from dance_studio.db.models import Staff, User


def _get_staff_by_user_or_telegram(db, user_id=None, telegram_id=None):
    staff = None
    if user_id:
        staff = db.query(Staff).filter_by(user_id=user_id, status="active").first()
    if not staff and telegram_id:
        staff = db.query(Staff).filter_by(telegram_id=telegram_id, status="active").first()
    return staff


def _get_current_staff(db):
    user_id = getattr(g, "user_id", None)
    telegram_id = getattr(g, "telegram_id", None)
    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    try:
        telegram_id = int(telegram_id) if telegram_id is not None else None
    except (TypeError, ValueError):
        telegram_id = None
    return _get_staff_by_user_or_telegram(db, user_id=user_id, telegram_id=telegram_id)

def get_telegram_user(optional: bool = True):
    telegram_id = getattr(g, "telegram_id", None)
    if not telegram_id:
        return ({"error": "auth required"}, 401) if not optional else None
    return {"id": telegram_id}


def _is_owner_or_admin(user_id, telegram_id):
    if user_id is not None:
        if TECH_ADMIN_ID and user_id == TECH_ADMIN_ID:
            return True
        if user_id in OWNER_IDS:
            return True
    if telegram_id is not None:
        if TECH_ADMIN_ID and telegram_id == TECH_ADMIN_ID:
            return True
        if telegram_id in OWNER_IDS:
            return True
    return False

def check_permission(telegram_id, permission, user_id=None):
    db = g.db
    staff = _get_staff_by_user_or_telegram(db, user_id=user_id, telegram_id=telegram_id)
    if not staff or not staff.position:
        return False
    staff_position = staff.position.strip().lower()
    return has_permission(staff_position, permission)


def require_permission(permission, allow_self_staff_id=None):
    telegram_id = getattr(g, "telegram_id", None)
    user_id = getattr(g, "user_id", None)

    if not telegram_id and not user_id:
        return {"error": "Требуется аутентификация"}, 401

    try:
        telegram_id = int(telegram_id) if telegram_id is not None else None
    except (TypeError, ValueError):
        return {"error": "Неверный telegram_id"}, 400

    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None

    # bypass для владельцев / техадмина даже без записи в staff
    if _is_owner_or_admin(user_id, telegram_id):
        return None

    if allow_self_staff_id is not None:
        db = g.db
        staff = _get_staff_by_user_or_telegram(db, user_id=user_id, telegram_id=telegram_id)
        if staff and staff.id == allow_self_staff_id:
            return None

    if not check_permission(telegram_id, permission, user_id=user_id):
        return {"error": "Нет прав доступа"}, 403

    return None

def get_current_user_from_request(db):
    user_id = getattr(g, "user_id", None)
    if user_id is not None:
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            user_id = None
        if user_id:
            user = db.query(User).filter_by(id=user_id).first()
            if user:
                return user

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
