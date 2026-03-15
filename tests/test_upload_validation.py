import io

import pytest
from werkzeug.datastructures import FileStorage

from dance_studio.web.services.upload_validation import (
    detect_image_extension,
    validate_image_upload,
)


JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBP_BYTES = b"RIFF\x1a\x00\x00\x00WEBPVP8 "


def _upload(data: bytes, *, filename: str, mimetype: str) -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=filename, content_type=mimetype)


def test_detect_image_extension():
    assert detect_image_extension(JPEG_BYTES) == ".jpg"
    assert detect_image_extension(PNG_BYTES) == ".png"
    assert detect_image_extension(WEBP_BYTES) == ".webp"
    assert detect_image_extension(b"not-an-image") is None


def test_validate_image_upload_accepts_valid_jpeg():
    upload = _upload(JPEG_BYTES, filename="avatar.jpeg", mimetype="image/jpeg")
    payload, ext = validate_image_upload(upload)
    assert ext == ".jpg"
    assert payload == JPEG_BYTES


def test_validate_image_upload_rejects_extension_signature_mismatch():
    upload = _upload(PNG_BYTES, filename="avatar.jpg", mimetype="image/jpeg")
    with pytest.raises(ValueError):
        validate_image_upload(upload)


def test_validate_image_upload_rejects_invalid_signature():
    upload = _upload(b"hello", filename="avatar.jpg", mimetype="image/jpeg")
    with pytest.raises(ValueError):
        validate_image_upload(upload)


def test_validate_image_upload_rejects_oversized_payload():
    upload = _upload(JPEG_BYTES + b"1234", filename="avatar.jpg", mimetype="image/jpeg")
    with pytest.raises(ValueError):
        validate_image_upload(upload, max_bytes=len(JPEG_BYTES))

