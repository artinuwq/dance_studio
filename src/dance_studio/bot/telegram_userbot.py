import asyncio
import os
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import RPCError
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

_lock = asyncio.Lock()


async def _get_client() -> TelegramClient:
    if not API_ID or not API_HASH:
        raise RuntimeError("TELEGRAM_API_ID/TELEGRAM_API_HASH not set for userbot")
    client = TelegramClient(SESSION_PATH, int(API_ID), API_HASH)
    await client.start()
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
