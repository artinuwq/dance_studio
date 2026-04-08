from datetime import datetime, timezone
from types import SimpleNamespace

from flask import Flask, g

from dance_studio.web.routes import admin as admin_routes


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._result

    def all(self):
        if self._result is None:
            return []
        if isinstance(self._result, list):
            return self._result
        return [self._result]


class _FakeDb:
    def __init__(self, *, staff=None, auth_identities=None):
        self._staff = staff
        self._auth_identities = auth_identities or []

    def query(self, model):
        if model is admin_routes.Staff:
            return _FakeQuery(self._staff)
        if model is admin_routes.AuthIdentity:
            return _FakeQuery(self._auth_identities)
        return _FakeQuery(None)


def _make_user(*, telegram_id=100500, photo_path=None):
    return SimpleNamespace(
        id=1,
        telegram_id=telegram_id,
        username="user",
        phone=None,
        name="User",
        email=None,
        birth_date=None,
        registered_at=datetime.now(timezone.utc),
        status="active",
        user_notes=None,
        staff_notes=None,
        photo_path=photo_path,
    )


def test_get_my_user_hides_local_photo_for_non_staff(monkeypatch):
    app = Flask(__name__)
    user = _make_user(photo_path="var/media/users/1/profile.jpg")
    monkeypatch.setattr(admin_routes, "get_current_user_from_request", lambda db: user)

    with app.test_request_context("/users/me", method="GET"):
        g.db = _FakeDb(staff=None)
        payload = admin_routes.get_my_user()

    assert isinstance(payload, dict)
    assert "photo_path" not in payload


def test_get_my_user_uses_staff_photo_path_for_staff(monkeypatch):
    app = Flask(__name__)
    user = _make_user(photo_path="var/media/users/1/profile.jpg")
    staff = SimpleNamespace(photo_path="var/media/teachers/7/photo.jpg")
    monkeypatch.setattr(admin_routes, "get_current_user_from_request", lambda db: user)

    with app.test_request_context("/users/me", method="GET"):
        g.db = _FakeDb(staff=staff)
        payload = admin_routes.get_my_user()

    assert isinstance(payload, dict)
    assert payload.get("photo_path") == "var/media/teachers/7/photo.jpg"


def test_get_my_user_falls_back_to_user_photo_for_staff(monkeypatch):
    app = Flask(__name__)
    user = _make_user(photo_path="var/media/users/1/profile.jpg")
    staff = SimpleNamespace(photo_path=None)
    monkeypatch.setattr(admin_routes, "get_current_user_from_request", lambda db: user)

    with app.test_request_context("/users/me", method="GET"):
        g.db = _FakeDb(staff=staff)
        payload = admin_routes.get_my_user()

    assert isinstance(payload, dict)
    assert payload.get("photo_path") == "var/media/users/1/profile.jpg"
