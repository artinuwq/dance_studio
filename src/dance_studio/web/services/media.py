from __future__ import annotations

import requests

from dance_studio.core.media_manager import save_user_photo
from dance_studio.db.models import User

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

__all__ = ["_build_image_url", "normalize_teaches", "try_fetch_telegram_avatar"]
