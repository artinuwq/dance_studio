from flask import Flask

from dance_studio.web.middleware import auth as auth_middleware
from dance_studio.web.services import auth_session


class _NoopDb:
    def close(self) -> None:
        return None


def _set_trusted(monkeypatch) -> None:
    monkeypatch.setattr(auth_session, "WEB_APP_URL", "https://app.example.com")
    monkeypatch.setattr(auth_session, "CSRF_TRUSTED_ORIGINS", "")


def _set_db(monkeypatch) -> None:
    monkeypatch.setattr(auth_middleware, "get_session", lambda: _NoopDb())


def test_state_changing_requires_sid_cookie_when_not_exempt(monkeypatch):
    _set_db(monkeypatch)
    _set_trusted(monkeypatch)
    app = Flask(__name__)

    with app.test_request_context(
        "/api/booking-requests",
        method="POST",
        headers={"Origin": "https://app.example.com"},
    ):
        assert auth_middleware.before_request() == ({"error": "auth required"}, 401)


def test_state_changing_exempt_path_allows_missing_sid(monkeypatch):
    _set_db(monkeypatch)
    _set_trusted(monkeypatch)
    app = Flask(__name__)

    with app.test_request_context(
        "/auth/telegram",
        method="POST",
        headers={"Origin": "https://app.example.com"},
    ):
        assert auth_middleware.before_request() is None


def test_state_changing_with_sid_still_requires_csrf_tokens(monkeypatch):
    _set_db(monkeypatch)
    _set_trusted(monkeypatch)
    app = Flask(__name__)

    with app.test_request_context(
        "/api/booking-requests",
        method="POST",
        headers={
            "Origin": "https://app.example.com",
            "Cookie": "sid=session123; csrf_token=abc123",
        },
    ):
        assert auth_middleware.before_request() == ({"error": "CSRF validation failed"}, 403)
