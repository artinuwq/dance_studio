from datetime import datetime, timezone
from types import SimpleNamespace

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
    def __init__(self, *, staff=None):
        self._staff = staff
        self.committed = False

    def query(self, model):
        if model is admin_routes.Staff:
            return _FakeQuery(self._staff)
        return _FakeQuery(None)

    def commit(self):
        self.committed = True


def _make_user():
    return SimpleNamespace(
        id=1,
        telegram_id=100500,
        username="user",
        phone=None,
        name="User",
        email=None,
        birth_date=None,
        registered_at=datetime.now(timezone.utc),
        status="active",
        user_notes=None,
        staff_notes=None,
        photo_path="var/media/users/1/profile.jpg",
    )


def test_update_my_user_updates_allowed_fields(monkeypatch):
    app = Flask(__name__)
    user = _make_user()
    db = _FakeDb(staff=None)
    monkeypatch.setattr(admin_routes, "get_current_user_from_request", lambda current_db: user)

    with app.test_request_context(
        "/users/me",
        method="PUT",
        json={"name": "New Name", "phone": "+79990000000", "birth_date": "2000-04-12", "user_notes": "note"},
    ):
        g.db = db
        payload = admin_routes.update_my_user()

    assert isinstance(payload, dict)
    assert db.committed is True
    assert payload["name"] == "New Name"
    assert payload["phone"] == "+79990000000"
    assert payload["birth_date"] == "2000-04-12"
    assert payload["user_notes"] == "note"


def test_update_my_user_rejects_invalid_birth_date(monkeypatch):
    app = Flask(__name__)
    user = _make_user()
    db = _FakeDb(staff=None)
    monkeypatch.setattr(admin_routes, "get_current_user_from_request", lambda current_db: user)

    with app.test_request_context("/users/me", method="PUT", json={"birth_date": "12.04.2000"}):
        g.db = db
        payload, status = admin_routes.update_my_user()

    assert status == 400
    assert payload == {"error": "birth_date must be YYYY-MM-DD"}
    assert db.committed is False


def test_users_me_route_accepts_put():
    app = Flask(__name__)
    app.register_blueprint(admin_routes.bp)

    methods = set()
    for rule in app.url_map.iter_rules():
        if rule.rule == "/users/me":
            methods.update(rule.methods)

    assert "PUT" in methods
