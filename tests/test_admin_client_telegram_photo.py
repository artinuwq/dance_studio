from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask, g

from dance_studio.web.routes import admin as admin_routes


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._result


class _FakeDb:
    def __init__(self, user):
        self._user = user

    def query(self, model):
        if model is admin_routes.User:
            return _FakeQuery(self._user)
        return _FakeQuery(None)


def _make_user(*, user_id=67, telegram_id=1376671577, photo_path=None):
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        photo_path=photo_path,
    )


def test_admin_client_telegram_photo_uses_local_photo_before_telegram_request(monkeypatch):
    app = Flask(__name__)
    media_root = (Path(".tmp") / "test-admin-client-telegram-photo" / uuid4().hex / "media").resolve()
    photo_file = media_root / "users" / "67" / "profile.jpg"
    photo_file.parent.mkdir(parents=True, exist_ok=True)
    photo_file.write_bytes(b"fake-image")

    user = _make_user(photo_path="var/media/users/67/profile.jpg")
    monkeypatch.setattr(admin_routes, "MEDIA_ROOT", media_root)
    monkeypatch.setattr(admin_routes, "require_permission", lambda permission: None)
    monkeypatch.setattr(admin_routes, "telegram_api_get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Telegram API should not be called")))
    monkeypatch.setattr(admin_routes, "telegram_api_download_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Telegram file download should not be called")))

    with app.test_request_context("/api/admin/clients/67/telegram-photo", method="GET"):
        g.db = _FakeDb(user)
        response = admin_routes.admin_get_client_telegram_photo(67)

    assert response.status_code == 200
    response.direct_passthrough = False
    assert response.get_data() == b"fake-image"
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"


def test_admin_client_telegram_photo_timeout_returns_424_without_exception_logging(monkeypatch):
    app = Flask(__name__)
    user = _make_user(photo_path=None)
    warning_calls = []
    exception_calls = []

    monkeypatch.setattr(admin_routes, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(admin_routes, "require_permission", lambda permission: None)
    monkeypatch.setattr(admin_routes, "telegram_api_get", lambda *args, **kwargs: (False, {}, "telegram_exception:timeout"))
    monkeypatch.setattr(app.logger, "warning", lambda *args, **kwargs: warning_calls.append((args, kwargs)))
    monkeypatch.setattr(app.logger, "exception", lambda *args, **kwargs: exception_calls.append((args, kwargs)))

    with app.test_request_context("/api/admin/clients/67/telegram-photo", method="GET"):
        g.db = _FakeDb(user)
        response = admin_routes.admin_get_client_telegram_photo(67)

    assert response[1] == 424
    assert response[0]["error"] == "Не удалось получить фото из Telegram"
    assert len(warning_calls) == 1
    assert exception_calls == []
