from flask import Flask

from dance_studio.web.services import auth_session


def _set_trusted(monkeypatch, *, web_app_url: str, trusted_origins: str) -> None:
    monkeypatch.setattr(auth_session, "WEB_APP_URL", web_app_url)
    monkeypatch.setattr(auth_session, "CSRF_TRUSTED_ORIGINS", trusted_origins)


def test_csrf_origin_only_passes_without_sid(monkeypatch):
    _set_trusted(monkeypatch, web_app_url="https://app.example.com", trusted_origins="")
    app = Flask(__name__)

    with app.test_request_context(
        "/api/test",
        method="POST",
        headers={"Origin": "https://app.example.com"},
    ):
        assert auth_session._is_csrf_valid() is True


def test_csrf_requires_double_submit_token_when_sid_present(monkeypatch):
    _set_trusted(monkeypatch, web_app_url="https://app.example.com", trusted_origins="")
    app = Flask(__name__)

    with app.test_request_context(
        "/api/test",
        method="POST",
        headers={
            "Origin": "https://app.example.com",
            "Cookie": "sid=session123; csrf_token=abc123",
            "X-CSRF-Token": "abc123",
        },
    ):
        assert auth_session._is_csrf_valid() is True


def test_csrf_rejects_missing_token_for_sid_session(monkeypatch):
    _set_trusted(monkeypatch, web_app_url="https://app.example.com", trusted_origins="")
    app = Flask(__name__)

    with app.test_request_context(
        "/api/test",
        method="POST",
        headers={
            "Origin": "https://app.example.com",
            "Cookie": "sid=session123; csrf_token=abc123",
        },
    ):
        assert auth_session._is_csrf_valid() is False


def test_csrf_rejects_origin_prefix_attack(monkeypatch):
    _set_trusted(monkeypatch, web_app_url="https://example.com", trusted_origins="")
    app = Flask(__name__)

    with app.test_request_context(
        "/api/test",
        method="POST",
        headers={"Origin": "https://example.com.attacker.com"},
    ):
        assert auth_session._is_csrf_valid() is False


def test_csrf_does_not_trust_current_host_implicitly(monkeypatch):
    _set_trusted(monkeypatch, web_app_url="", trusted_origins="")
    app = Flask(__name__)

    with app.test_request_context(
        "/api/test",
        method="POST",
        base_url="https://local.example.com",
        headers={"Origin": "https://local.example.com"},
    ):
        assert auth_session._is_csrf_valid() is False
