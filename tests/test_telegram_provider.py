from __future__ import annotations

from dance_studio.notifications.providers.telegram import TelegramNotificationProvider
import dance_studio.notifications.providers.telegram as telegram_provider


def test_telegram_provider_uses_transport_helper(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(method: str, payload: dict, *, timeout: int = 10):
        captured["method"] = method
        captured["payload"] = payload
        captured["timeout"] = timeout
        return True, {"result": {"message_id": 42}}, None

    monkeypatch.setattr(telegram_provider, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(telegram_provider, "telegram_api_post", _fake_post)

    provider = TelegramNotificationProvider()
    result = provider.send("12345", "title", "body", {"parse_mode": "Markdown"})

    assert result["ok"] is True
    assert result["provider_message_id"] == "tg:42"
    assert captured["method"] == "sendMessage"
    assert captured["timeout"] == 15
    assert captured["payload"]["chat_id"] == 12345
    assert captured["payload"]["text"] == "body"
    assert captured["payload"]["parse_mode"] == "Markdown"
