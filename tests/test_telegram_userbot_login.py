import asyncio
from types import SimpleNamespace

import pytest
import socks

import dance_studio.bot.telegram_userbot as userbot


class _FakeTelegramClient:
    authorized = False
    require_password = False
    invalid_password = False
    sent_phone = None
    signed_in_phone = None
    signed_in_code = None
    signed_in_phone_code_hash = None
    signed_in_password = None
    connect_proxies = None
    fail_proxy_types = None

    def __init__(self, *args, **kwargs):
        self.disconnected = False
        self.proxy = kwargs.get("proxy")

    async def connect(self):
        connect_proxies = type(self).connect_proxies
        if connect_proxies is not None:
            connect_proxies.append(self.proxy)
        if self.proxy is not None and type(self).fail_proxy_types and self.proxy[0] in type(self).fail_proxy_types:
            raise TimeoutError("proxy timeout")
        return None

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return bool(type(self).authorized)

    async def get_me(self):
        return SimpleNamespace(username="restored_userbot", first_name="Restored", id=777)

    async def send_code_request(self, phone):
        type(self).sent_phone = phone
        return SimpleNamespace(phone_code_hash="hash-123")

    async def sign_in(self, *, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            type(self).signed_in_password = password
            if type(self).invalid_password:
                raise userbot.PasswordHashInvalidError()
            type(self).authorized = True
            return await self.get_me()
        type(self).signed_in_phone = phone
        type(self).signed_in_code = code
        type(self).signed_in_phone_code_hash = phone_code_hash
        if type(self).require_password:
            raise userbot.SessionPasswordNeededError()
        type(self).authorized = True
        return await self.get_me()


@pytest.fixture(autouse=True)
def _patch_userbot(monkeypatch):
    class _FakeSessionPasswordNeededError(Exception):
        pass

    class _FakePasswordHashInvalidError(Exception):
        pass

    _FakeTelegramClient.authorized = False
    _FakeTelegramClient.require_password = False
    _FakeTelegramClient.invalid_password = False
    _FakeTelegramClient.sent_phone = None
    _FakeTelegramClient.signed_in_phone = None
    _FakeTelegramClient.signed_in_code = None
    _FakeTelegramClient.signed_in_phone_code_hash = None
    _FakeTelegramClient.signed_in_password = None
    _FakeTelegramClient.connect_proxies = []
    _FakeTelegramClient.fail_proxy_types = set()
    monkeypatch.setattr(userbot, "API_ID", "12345")
    monkeypatch.setattr(userbot, "API_HASH", "hash")
    monkeypatch.setattr(userbot, "SESSION_PATH", "var/sessions/userbot.session")
    monkeypatch.setattr(userbot, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(userbot, "SessionPasswordNeededError", _FakeSessionPasswordNeededError)
    monkeypatch.setattr(userbot, "PasswordHashInvalidError", _FakePasswordHashInvalidError)
    yield


def test_request_login_code_normalizes_phone_and_returns_hash():
    result = asyncio.run(userbot.request_login_code("8 (999) 123-45-67"))

    assert result["ok"] is True
    assert result["already_authorized"] is False
    assert result["phone"] == "+79991234567"
    assert result["phone_code_hash"] == "hash-123"
    assert _FakeTelegramClient.sent_phone == "+79991234567"


def test_request_login_code_returns_existing_authorized_account():
    _FakeTelegramClient.authorized = True

    result = asyncio.run(userbot.request_login_code("+79991234567"))

    assert result["ok"] is True
    assert result["already_authorized"] is True
    assert result["identity"] == "@restored_userbot"


def test_complete_login_code_requests_password_when_2fa_enabled():
    _FakeTelegramClient.require_password = True

    with pytest.raises(userbot.UserbotPasswordRequiredError):
        asyncio.run(userbot.complete_login_code("+79991234567", "12345", "hash-123"))


def test_complete_login_password_authorizes_userbot():
    result = asyncio.run(userbot.complete_login_password("secret-pass"))

    assert result["ok"] is True
    assert result["identity"] == "@restored_userbot"
    assert _FakeTelegramClient.signed_in_password == "secret-pass"


def test_resolve_userbot_proxy_parses_socks5_url():
    proxy = userbot.resolve_userbot_proxy("socks5://proxy-user:proxy-pass@127.0.0.1:1080")

    assert proxy == (socks.SOCKS5, "127.0.0.1", 1080, True, "proxy-user", "proxy-pass")


def test_resolve_userbot_proxy_candidates_add_http_fallback_for_socks_url():
    proxies = userbot.resolve_userbot_proxy_candidates("socks5://proxy-user:proxy-pass@127.0.0.1:1080")

    assert proxies == [
        (socks.SOCKS5, "127.0.0.1", 1080, True, "proxy-user", "proxy-pass"),
        (socks.HTTP, "127.0.0.1", 1080, False, "proxy-user", "proxy-pass"),
    ]


def test_request_login_code_retries_with_http_proxy_when_socks_proxy_times_out(monkeypatch):
    monkeypatch.setattr(userbot, "USERBOT_PROXY", "socks5://proxy-user:proxy-pass@127.0.0.1:1080")
    _FakeTelegramClient.fail_proxy_types = {socks.SOCKS5}

    result = asyncio.run(userbot.request_login_code("+79991234567"))

    assert result["ok"] is True
    assert _FakeTelegramClient.connect_proxies == [
        (socks.SOCKS5, "127.0.0.1", 1080, True, "proxy-user", "proxy-pass"),
        (socks.HTTP, "127.0.0.1", 1080, False, "proxy-user", "proxy-pass"),
    ]
