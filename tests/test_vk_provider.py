from __future__ import annotations

import dance_studio.notifications.providers.vk as vk_provider
from dance_studio.notifications.providers.vk import VkNotificationProvider, edit_vk_message


class _FakeResponse:
    ok = True
    status_code = 200
    content = b'{"response": 42}'

    def json(self):
        return {"response": 42}


def test_vk_provider_send_returns_message_id_and_keyboard(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(url, data=None, timeout=0):
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(vk_provider, "VK_COMMUNITY_ACCESS_TOKEN", "vk-token")
    monkeypatch.setattr(vk_provider.requests, "post", _fake_post)

    provider = VkNotificationProvider()
    result = provider.send(
        "778899",
        "",
        "Reminder",
        {"keyboard": {"inline": True, "buttons": []}},
    )

    assert result == {"ok": True, "provider_message_id": "vk:42", "message_id": 42}
    assert captured["url"] == "https://api.vk.com/method/messages.send"
    assert captured["timeout"] == 10
    assert captured["data"]["user_id"] == 778899
    assert captured["data"]["message"] == "Reminder"
    assert captured["data"]["keyboard"] == '{"inline":true,"buttons":[]}'


def test_edit_vk_message_posts_messages_edit_payload(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_post(url, data=None, timeout=0):
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(vk_provider, "VK_COMMUNITY_ACCESS_TOKEN", "vk-token")
    monkeypatch.setattr(vk_provider.requests, "post", _fake_post)

    result = edit_vk_message(
        peer_id=778899,
        message_id=42,
        message="Updated reminder",
        payload={"keyboard": {"inline": True, "buttons": []}},
    )

    assert result == {"ok": True}
    assert captured["url"] == "https://api.vk.com/method/messages.edit"
    assert captured["timeout"] == 10
    assert captured["data"]["peer_id"] == 778899
    assert captured["data"]["message_id"] == 42
    assert captured["data"]["message"] == "Updated reminder"
    assert captured["data"]["keyboard"] == '{"inline":true,"buttons":[]}'
