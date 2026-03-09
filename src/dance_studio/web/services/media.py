from __future__ import annotations

import requests

from dance_studio.core.media_manager import save_user_photo


def _build_image_url(path: str | None) -> str | None:
    """
    Normalizes stored relative paths (var/media/..., database/media/...)
    into HTTP URL served by /media/<path:...>.
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
        if v in ("1", "true", "yes", "y", "да"):
            return 1
        if v in ("0", "false", "no", "n", "нет"):
            return 0
    return None


def try_fetch_telegram_avatar(telegram_id, db, staff_obj=None):
    """
    Attempts to download Telegram profile photo and persist it for staff.
    Client avatars should be consumed via Telegram API proxy only.
    """
    try:
        from dance_studio.core.config import BOT_TOKEN
    except Exception:
        return

    if staff_obj is None or getattr(staff_obj, "photo_path", None):
        return

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
            params={"user_id": telegram_id, "limit": 1},
            timeout=5,
        )
        data = resp.json()
        if not data.get("ok") or data.get("result", {}).get("total_count", 0) == 0:
            return

        file_id = data["result"]["photos"][0][-1]["file_id"]
        file_resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=5,
        )
        file_data = file_resp.json()
        if not file_data.get("ok"):
            return

        file_path = file_data["result"]["file_path"]
        photo_resp = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
            timeout=10,
        )
        if photo_resp.status_code != 200:
            return

        storage_id = getattr(staff_obj, "id", None) or telegram_id
        photo_path = save_user_photo(storage_id, photo_resp.content)
        if not photo_path:
            return

        staff_obj.photo_path = photo_path
        db.commit()
    except Exception:
        # No hard failure on network issues.
        return


__all__ = ["_build_image_url", "normalize_teaches", "try_fetch_telegram_avatar"]
