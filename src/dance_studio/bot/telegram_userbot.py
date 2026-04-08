import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import socks
from telethon import TelegramClient
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.tl import functions, types


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        if k and k not in os.environ:
            os.environ[k.strip()] = v.strip()


_load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VAR_ROOT = PROJECT_ROOT / "var"
SESSION_DIR = VAR_ROOT / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

API_ID = os.getenv("TELEGRAM_API_ID") or os.getenv("TG_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH") or os.getenv("TG_API_HASH")
SESSION_PATH = os.getenv("TELEGRAM_SESSION") or os.getenv("TG_SESSION") or str(SESSION_DIR / "userbot.session")
USERBOT_PROXY = (os.getenv("TELEGRAM_PROXY") or os.getenv("BACKUP_TELEGRAM_PROXY") or "").strip()
USERBOT_CONNECT_TIMEOUT_SECONDS = 10
USERBOT_REQUEST_RETRIES = 1
USERBOT_CONNECTION_RETRIES = 1
USERBOT_RETRY_DELAY_SECONDS = 0

_lock = asyncio.Lock()


class UserbotSessionNotAuthorizedError(RuntimeError):
    pass


class UserbotPhoneNumberInvalidError(RuntimeError):
    pass


class UserbotPhoneCodeInvalidError(RuntimeError):
    pass


class UserbotPhoneCodeExpiredError(RuntimeError):
    pass


class UserbotPasswordRequiredError(RuntimeError):
    pass


class UserbotPasswordInvalidError(RuntimeError):
    pass


def _normalize_login_phone(phone: str | None) -> str | None:
    raw = str(phone or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    if raw.startswith("+"):
        normalized = f"+{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        normalized = f"+7{digits[1:]}"
    elif len(digits) == 10:
        normalized = f"+7{digits}"
    else:
        normalized = f"+{digits}"
    return normalized if len(normalized) >= 8 else None


def _format_account_identity(me: Any) -> str:
    username = str(getattr(me, "username", "") or "").strip()
    if username:
        return f"@{username}"
    first_name = str(getattr(me, "first_name", "") or "").strip()
    if first_name:
        return first_name
    user_id = getattr(me, "id", None)
    return f"id={user_id}" if user_id is not None else "аккаунт"


def _build_userbot_proxy_candidate(
    scheme: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> tuple[Any, ...]:
    proxy_type = {
        "socks5": socks.SOCKS5,
        "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }.get(str(scheme or "").strip().lower())
    if proxy_type is None:
        raise RuntimeError(f"Unsupported TELEGRAM_PROXY scheme for userbot: {scheme or 'missing'}")
    rdns = scheme in {"socks5", "socks5h", "socks4"}
    return (proxy_type, host, int(port), rdns, username, password)


def resolve_userbot_proxy_candidates(raw_proxy: str | None = None) -> list[tuple[Any, ...] | None]:
    proxy_value = str(USERBOT_PROXY if raw_proxy is None else raw_proxy or "").strip()
    if not proxy_value:
        return [None]

    parsed = urlparse(proxy_value)
    scheme = str(parsed.scheme or "").strip().lower()
    host = str(parsed.hostname or "").strip()
    port = parsed.port
    if not host or port is None:
        raise RuntimeError("TELEGRAM_PROXY must include host and port for userbot")

    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    candidates: list[tuple[Any, ...] | None] = [
        _build_userbot_proxy_candidate(scheme, host, int(port), username, password)
    ]
    if scheme in {"socks5", "socks5h", "socks4"}:
        candidates.append(_build_userbot_proxy_candidate("http", host, int(port), username, password))

    unique_candidates: list[tuple[Any, ...] | None] = []
    for candidate in candidates:
        if candidate in unique_candidates:
            continue
        unique_candidates.append(candidate)
    return unique_candidates


def resolve_userbot_proxy(raw_proxy: str | None = None) -> tuple[Any, ...] | None:
    return resolve_userbot_proxy_candidates(raw_proxy)[0]


def _build_client(
    *,
    session_path: str | None = None,
    api_id: str | None = None,
    api_hash: str | None = None,
    proxy: tuple[Any, ...] | dict[str, Any] | None = None,
) -> TelegramClient:
    resolved_api_id = str(API_ID if api_id is None else api_id).strip()
    resolved_api_hash = str(API_HASH if api_hash is None else api_hash).strip()
    resolved_session_path = SESSION_PATH if session_path is None else session_path
    resolved_proxy = resolve_userbot_proxy() if proxy is None else proxy

    if not resolved_api_id or not resolved_api_hash:
        raise RuntimeError("TELEGRAM_API_ID/TELEGRAM_API_HASH not set for userbot")
    return TelegramClient(
        resolved_session_path,
        int(resolved_api_id),
        resolved_api_hash,
        proxy=resolved_proxy,
        timeout=USERBOT_CONNECT_TIMEOUT_SECONDS,
        request_retries=USERBOT_REQUEST_RETRIES,
        connection_retries=USERBOT_CONNECTION_RETRIES,
        retry_delay=USERBOT_RETRY_DELAY_SECONDS,
        auto_reconnect=False,
    )


async def _connect_client() -> TelegramClient:
    last_error: Exception | None = None
    for proxy_candidate in resolve_userbot_proxy_candidates():
        client = _build_client(proxy=proxy_candidate)
        try:
            await client.connect()
            return client
        except Exception as exc:
            last_error = exc
            try:
                await client.disconnect()
            except Exception:
                pass
    if last_error is not None:
        raise last_error
    raise RuntimeError("userbot_connect_failed")


async def _get_client() -> TelegramClient:
    client = await _connect_client()
    if not await client.is_user_authorized():
        try:
            await client.disconnect()
        except Exception:
            pass
        raise UserbotSessionNotAuthorizedError("userbot_session_not_authorized")
    return client


def _normalize_user(user: Any) -> dict:
    if isinstance(user, dict):
        return {
            "id": user.get("id"),
            "username": user.get("username"),
            "phone": user.get("phone"),
            "name": user.get("name") or user.get("first_name"),
        }
    if isinstance(user, int):
        return {"id": user}
    if isinstance(user, str):
        if user.startswith("@"):
            return {"username": user[1:]}
        if user.isdigit():
            return {"id": int(user)}
        return {"username": user}
    return {"id": None}


async def _resolve_user_entity(client: TelegramClient, user: Any):
    u = _normalize_user(user)
    uid = u.get("id")
    username = u.get("username")
    phone = u.get("phone")
    display_name = u.get("name") or (f"user{uid}" if uid else "contact")

    if username:
        try:
            return await client.get_input_entity(f"@{username}")
        except Exception:
            pass

    if phone:
        try:
            contact = types.InputPhoneContact(
                client_id=0,
                phone=phone,
                first_name=display_name,
                last_name="",
            )
            res = await client(functions.contacts.ImportContactsRequest([contact]))
            if res.users:
                return await client.get_input_entity(res.users[0])
        except Exception:
            pass

    if uid:
        try:
            return await client.get_input_entity(types.PeerUser(int(uid)))
        except Exception:
            pass

    raise RuntimeError(
        f"Cannot resolve Telegram entity for user (id={uid}, username={username}, phone={phone})"
    )


async def send_private_message(user: Any, text: str) -> dict:
    async with _lock:
        client = await _get_client()
        try:
            entity = await _resolve_user_entity(client, user)
            await client.send_message(entity=entity, message=text)
            return {"ok": True}
        except RPCError as exc:
            raise RuntimeError(f"Telethon RPC error: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


async def request_login_code(phone: str) -> dict:
    normalized_phone = _normalize_login_phone(phone)
    if not normalized_phone:
        raise UserbotPhoneNumberInvalidError("userbot_phone_invalid")

    async with _lock:
        client = await _connect_client()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                return {
                    "ok": True,
                    "already_authorized": True,
                    "phone": normalized_phone,
                    "identity": _format_account_identity(me),
                }
            sent = await client.send_code_request(normalized_phone)
            return {
                "ok": True,
                "phone": normalized_phone,
                "phone_code_hash": sent.phone_code_hash,
                "already_authorized": False,
            }
        except PhoneNumberInvalidError as exc:
            raise UserbotPhoneNumberInvalidError("userbot_phone_invalid") from exc
        except RPCError as exc:
            raise RuntimeError(f"Telethon RPC error: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


async def complete_login_code(phone: str, code: str, phone_code_hash: str) -> dict:
    normalized_phone = _normalize_login_phone(phone)
    normalized_code = "".join(ch for ch in str(code or "").strip() if ch.isdigit())
    if not normalized_phone:
        raise UserbotPhoneNumberInvalidError("userbot_phone_invalid")
    if not normalized_code:
        raise UserbotPhoneCodeInvalidError("userbot_code_invalid")
    if not str(phone_code_hash or "").strip():
        raise RuntimeError("userbot_phone_code_hash_missing")

    async with _lock:
        client = await _connect_client()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                return {
                    "ok": True,
                    "identity": _format_account_identity(me),
                    "already_authorized": True,
                }
            me = await client.sign_in(phone=normalized_phone, code=normalized_code, phone_code_hash=phone_code_hash)
            if me is None:
                me = await client.get_me()
            return {
                "ok": True,
                "identity": _format_account_identity(me),
                "already_authorized": False,
            }
        except SessionPasswordNeededError as exc:
            raise UserbotPasswordRequiredError("userbot_password_required") from exc
        except PhoneCodeInvalidError as exc:
            raise UserbotPhoneCodeInvalidError("userbot_code_invalid") from exc
        except PhoneCodeExpiredError as exc:
            raise UserbotPhoneCodeExpiredError("userbot_code_expired") from exc
        except RPCError as exc:
            raise RuntimeError(f"Telethon RPC error: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


async def complete_login_password(password: str) -> dict:
    normalized_password = str(password or "")
    if not normalized_password:
        raise UserbotPasswordInvalidError("userbot_password_invalid")

    async with _lock:
        client = await _connect_client()
        try:
            me = await client.sign_in(password=normalized_password)
            if me is None:
                if not await client.is_user_authorized():
                    raise UserbotSessionNotAuthorizedError("userbot_session_not_authorized")
                me = await client.get_me()
            return {
                "ok": True,
                "identity": _format_account_identity(me),
            }
        except PasswordHashInvalidError as exc:
            raise UserbotPasswordInvalidError("userbot_password_invalid") from exc
        except RPCError as exc:
            raise RuntimeError(f"Telethon RPC error: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


def send_private_message_sync(user: Any, text: str) -> dict | None:
    try:
        return asyncio.run(send_private_message(user, text))
    except Exception as exc:
        reason = str(exc).strip() or repr(exc)
        print(f"Failed to send userbot message: {reason}")
        return {"ok": False, "error": reason}


async def _login_cli():
    client = await _get_client()
    me = await client.get_me()
    print(f"Userbot session ready as @{me.username or me.first_name}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(_login_cli())
