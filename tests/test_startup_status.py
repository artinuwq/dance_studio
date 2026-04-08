from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dance_studio.bot.startup_status import (
    build_startup_status_text,
    describe_tech_status_target,
    describe_userbot_runtime_status,
    describe_userbot_status,
    describe_vk_community_status,
    describe_vk_mini_app_status,
)


def test_describe_userbot_status_reports_missing_config(tmp_path):
    session_path = tmp_path / "userbot.session"
    result = describe_userbot_status(api_id="", api_hash="", session_path=str(session_path))
    assert result.startswith("User-bot: ")
    assert "TELEGRAM_API_ID/TELEGRAM_API_HASH" in result


def test_describe_userbot_status_reports_ready_session(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=16))
    result = describe_userbot_status(
        api_id="12345",
        api_hash="hash",
        session_path="var/sessions/userbot.session",
    )
    assert result.startswith("User-bot: ")
    assert "session=userbot.session" in result


def test_describe_userbot_runtime_status_reports_reset_session(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=16))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return False

        async def disconnect(self):
            return None

    monkeypatch.setattr("dance_studio.bot.startup_status.TelegramClient", FakeClient)
    result = asyncio.run(
        describe_userbot_runtime_status(
            api_id="12345",
            api_hash="hash",
            session_path="var/sessions/userbot.session",
        )
    )
    assert result.startswith("User-bot: ")
    assert "session=userbot.session" in result


def test_describe_userbot_runtime_status_reports_connected_account(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=16))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(username="real_userbot", id=777)

        async def disconnect(self):
            return None

    monkeypatch.setattr("dance_studio.bot.startup_status.TelegramClient", FakeClient)
    result = asyncio.run(
        describe_userbot_runtime_status(
            api_id="12345",
            api_hash="hash",
            session_path="var/sessions/userbot.session",
        )
    )
    assert result.endswith("(@real_userbot)")


def test_describe_userbot_runtime_status_passes_proxy_to_telethon(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=16))
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["proxy"] = kwargs.get("proxy")

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(username="real_userbot", id=777)

        async def disconnect(self):
            return None

    monkeypatch.setattr("dance_studio.bot.startup_status.TelegramClient", FakeClient)
    monkeypatch.setattr("dance_studio.bot.startup_status.resolve_userbot_proxy_candidates", lambda: [("proxy",)])
    result = asyncio.run(
        describe_userbot_runtime_status(
            api_id="12345",
            api_hash="hash",
            session_path="var/sessions/userbot.session",
        )
    )

    assert result.endswith("(@real_userbot)")
    assert captured["proxy"] == ("proxy",)


def test_describe_userbot_runtime_status_retries_with_proxy_fallback(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=16))
    attempts: list[object] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.proxy = kwargs.get("proxy")

        async def connect(self):
            attempts.append(self.proxy)
            if self.proxy == ("bad",):
                raise TimeoutError("proxy timeout")
            return None

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(username="real_userbot", id=777)

        async def disconnect(self):
            return None

    monkeypatch.setattr("dance_studio.bot.startup_status.TelegramClient", FakeClient)
    monkeypatch.setattr("dance_studio.bot.startup_status.resolve_userbot_proxy_candidates", lambda: [("bad",), ("good",)])
    result = asyncio.run(
        describe_userbot_runtime_status(
            api_id="12345",
            api_hash="hash",
            session_path="var/sessions/userbot.session",
        )
    )

    assert result.endswith("(@real_userbot)")
    assert attempts == [("bad",), ("good",)]


def test_describe_vk_mini_app_status_reports_partial_config():
    result = describe_vk_mini_app_status(app_id="12345", service_key="", secret_key="secret")
    assert result.startswith("VK Mini App: ")
    assert "app_id=12345" in result
    assert "service_key" in result


def test_describe_vk_community_status_reports_group_and_token():
    result = describe_vk_community_status(community_id="442972788", access_token="token")
    assert result.startswith("VK ")
    assert "group_id=442972788" in result
    assert "token=ok" in result


def test_describe_tech_status_target_reports_runtime_ids():
    result = describe_tech_status_target(chat_id=-1001234567890, topic_id=77)
    assert "chat_id=-1001234567890" in result
    assert "topic_id=77" in result


def test_build_startup_status_text_contains_summary_lines():
    text = build_startup_status_text(
        started_at=datetime(2026, 4, 5, 16, 56, 54),
        bot_username="sheba_bot",
        tech_chat_id=-100555000111,
        tech_status_topic_id=12,
        userbot_status_line="User-bot: Ð Ñ—Ð Ñ•Ð Ò‘Ð Ñ”Ð Â»Ð¡Ð‹Ð¡â€¡Ð ÂµÐ Ð… (@real_userbot)",
    )

    assert "05.04.2026 16:56:54" in text
    assert "@sheba_bot" in text
    assert "chat_id=-100555000111" in text
    assert "topic_id=12" in text
    assert "(@real_userbot)" in text
    assert "VK Mini App:" in text
    assert "group_id=" in text
