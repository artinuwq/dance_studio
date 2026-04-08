from __future__ import annotations

from dance_studio.core.media_manager import save_user_photo
from dance_studio.core.telegram_http import telegram_api_download_file, telegram_api_get


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
        profile_ok, profile_data, _ = telegram_api_get(
            "getUserProfilePhotos",
            {"user_id": telegram_id, "limit": 1},
            timeout=15,
        )
        if not profile_ok:
            return

        photos = (profile_data.get("result") or {}).get("photos") or []
        if not photos or not photos[0]:
            return

        file_id = (photos[0][-1] or {}).get("file_id")
        if not file_id:
            return

        file_ok, file_data, _ = telegram_api_get(
            "getFile",
            {"file_id": file_id},
            timeout=15,
        )
        if not file_ok:
            return

        file_path = str(((file_data.get("result") or {}).get("file_path") or "")).strip()
        if not file_path:
            return

        image_ok, image_bytes, _, _ = telegram_api_download_file(
            file_path,
            timeout=15,
        )
        if not image_ok or not image_bytes:
            return

        storage_id = getattr(staff_obj, "id", None) or telegram_id
        photo_path = save_user_photo(storage_id, image_bytes)
        if not photo_path:
            return

        staff_obj.photo_path = photo_path
        db.commit()
    except Exception:
        # No hard failure on network issues.
        return


__all__ = ["_build_image_url", "normalize_teaches", "try_fetch_telegram_avatar"]
