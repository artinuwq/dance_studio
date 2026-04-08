from __future__ import annotations

import dance_studio.core.telegram_http as telegram_http


class _FakeResponse:
    ok = True
    status_code = 200
    content = b'{"ok": true, "result": {"message_id": 15}}'
    text = '{"ok": true}'
    headers = {"Content-Type": "application/json"}

    def json(self):
        return {"ok": True, "result": {"message_id": 15}}


def test_telegram_api_post_uses_explicit_proxy_without_env(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self):
            self.trust_env = True
            captured["session"] = self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeResponse()

    monkeypatch.setattr(telegram_http, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(telegram_http, "TELEGRAM_PROXY", "socks5://proxy.example:1080")
    monkeypatch.setattr(telegram_http, "BACKUP_TELEGRAM_PROXY", "")
    monkeypatch.setattr(telegram_http.requests, "Session", _FakeSession)

    ok, data, error = telegram_http.telegram_api_post(
        "sendMessage",
        {"chat_id": 1, "text": "hello"},
        timeout=7,
    )

    assert ok is True
    assert error is None
    assert data["ok"] is True
    assert captured["url"] == "https://api.telegram.org/bottest-token/sendMessage"
    assert captured["session"].trust_env is False
    assert captured["kwargs"]["timeout"] == 7
    assert captured["kwargs"]["json"] == {"chat_id": 1, "text": "hello"}
    assert captured["kwargs"]["proxies"] == {
        "http": "socks5h://proxy.example:1080",
        "https": "socks5h://proxy.example:1080",
    }


def test_telegram_api_get_uses_explicit_proxy_without_env(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self):
            self.trust_env = True
            captured["session"] = self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeResponse()

    monkeypatch.setattr(telegram_http, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(telegram_http, "TELEGRAM_PROXY", "socks5://proxy.example:1080")
    monkeypatch.setattr(telegram_http, "BACKUP_TELEGRAM_PROXY", "")
    monkeypatch.setattr(telegram_http.requests, "Session", _FakeSession)

    ok, data, error = telegram_http.telegram_api_get(
        "getFile",
        {"file_id": "abc"},
        timeout=9,
    )

    assert ok is True
    assert error is None
    assert data["ok"] is True
    assert captured["url"] == "https://api.telegram.org/bottest-token/getFile"
    assert captured["session"].trust_env is False
    assert captured["kwargs"]["timeout"] == 9
    assert captured["kwargs"]["params"] == {"file_id": "abc"}
    assert captured["kwargs"]["proxies"] == {
        "http": "socks5h://proxy.example:1080",
        "https": "socks5h://proxy.example:1080",
    }


def test_telegram_api_download_file_uses_proxy_and_returns_bytes(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeBinaryResponse:
        ok = True
        status_code = 200
        content = b"binary-image"
        text = ""
        headers = {"Content-Type": "image/jpeg"}

    class _FakeSession:
        def __init__(self):
            self.trust_env = True
            captured["session"] = self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeBinaryResponse()

    monkeypatch.setattr(telegram_http, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(telegram_http, "TELEGRAM_PROXY", "socks5://proxy.example:1080")
    monkeypatch.setattr(telegram_http, "BACKUP_TELEGRAM_PROXY", "")
    monkeypatch.setattr(telegram_http.requests, "Session", _FakeSession)

    ok, content, content_type, error = telegram_http.telegram_api_download_file(
        "photos/avatar.jpg",
        timeout=11,
    )

    assert ok is True
    assert error is None
    assert content == b"binary-image"
    assert content_type == "image/jpeg"
    assert captured["url"] == "https://api.telegram.org/file/bottest-token/photos/avatar.jpg"
    assert captured["session"].trust_env is False
    assert captured["kwargs"]["timeout"] == 11
    assert captured["kwargs"]["proxies"] == {
        "http": "socks5h://proxy.example:1080",
        "https": "socks5h://proxy.example:1080",
    }
