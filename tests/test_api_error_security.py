from pathlib import Path

from flask import Flask

from dance_studio.web.middleware import errors as errors_middleware
from dance_studio.web.middleware.errors import register_error_handlers
from dance_studio.core import group_notifications
from dance_studio.web.services.api_errors import safe_client_error_message, token_fingerprint


def test_safe_client_error_message_strips_key_error_quotes():
    assert safe_client_error_message(KeyError("missing_key")) == "missing_key"


def test_token_fingerprint_masks_token():
    token = "secret-session-token"

    fingerprint = token_fingerprint(token)

    assert fingerprint != token
    assert len(fingerprint) == 12
    assert fingerprint == token_fingerprint(token)


def test_unhandled_exception_response_hides_trace_and_exception(monkeypatch):
    monkeypatch.setattr(errors_middleware, "send_critical_sync", lambda message: None)

    app = Flask(__name__)
    register_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("database password=secret")

    response = app.test_client().get("/boom")

    assert response.status_code == 500
    assert response.get_json() == {"error": "internal_server_error"}


def test_group_notification_error_is_generic(monkeypatch):
    class _FakeService:
        def send(self, db, *, user_id, event_type, title, body, payload):
            raise RuntimeError("provider BOT_TOKEN=secret")

    monkeypatch.setattr(group_notifications, "NotificationService", lambda: _FakeService())

    result = group_notifications.send_group_notifications(
        object(),
        recipient_user_ids=[123456],
        event_type="group_deleted",
        title="T",
        body="B",
    )

    assert result["sent_count"] == 0
    assert result["error"] == "group_notification_delivery_failed"


def test_requirements_are_version_pinned():
    repo_root = Path(__file__).resolve().parents[1]

    for requirements_name in ("requirements.txt", "requirements-dev.txt"):
        requirements_path = repo_root / requirements_name
        lines = [
            line.strip()
            for line in requirements_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        assert lines
        assert all("==" in line or line.startswith("-r ") for line in lines)


def test_upload_token_logs_do_not_contain_raw_token_patterns():
    repo_root = Path(__file__).resolve().parents[1]
    bot_text = (repo_root / "src" / "dance_studio" / "bot" / "bot.py").read_text(encoding="utf-8")
    admin_text = (repo_root / "src" / "dance_studio" / "web" / "routes" / "admin.py").read_text(encoding="utf-8")

    assert "DEBUG: token =" not in bot_text
    assert "DEBUG: start_param =" not in bot_text
    assert "token=%s" not in admin_text
    assert "token_fp=%s" in admin_text
