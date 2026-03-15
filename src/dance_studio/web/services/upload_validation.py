from __future__ import annotations

from pathlib import Path
from typing import Any

from dance_studio.web.constants import MAX_UPLOAD_MB


MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_EXT_ALIAS = {".jpeg": ".jpg"}
_ALLOWED_MIME_BY_EXT = {
    ".jpg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
}


def detect_image_extension(data: bytes) -> str | None:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _normalized_extension(filename: str) -> str:
    extension = Path(filename or "").suffix.lower()
    extension = _EXT_ALIAS.get(extension, extension)
    return extension


def _read_upload_bytes(upload: Any, max_bytes: int) -> bytes:
    data = upload.read(max_bytes + 1)
    try:
        upload.seek(0)
    except Exception:
        pass
    return data


def validate_image_upload(upload: Any, *, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[bytes, str]:
    filename = str(getattr(upload, "filename", "") or "")
    extension = _normalized_extension(filename)
    if extension not in {".jpg", ".png", ".webp"}:
        raise ValueError("Поддерживаются только JPG/PNG/WEBP")

    data = _read_upload_bytes(upload, max_bytes)
    if not data:
        raise ValueError("Файл пустой")
    if len(data) > max_bytes:
        raise ValueError(f"Размер файла превышает {MAX_UPLOAD_MB} MB")

    detected_extension = detect_image_extension(data)
    if not detected_extension:
        raise ValueError("Неподдерживаемая сигнатура файла")
    if detected_extension != extension:
        raise ValueError("Расширение файла не совпадает с содержимым")

    mime = str(getattr(upload, "mimetype", "") or "").lower().strip()
    if mime and mime not in _ALLOWED_MIME_BY_EXT[detected_extension]:
        raise ValueError("MIME-тип файла не совпадает с содержимым")

    return data, detected_extension


__all__ = [
    "ALLOWED_IMAGE_EXTENSIONS",
    "MAX_UPLOAD_BYTES",
    "detect_image_extension",
    "validate_image_upload",
]
