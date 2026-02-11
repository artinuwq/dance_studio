import asyncio
import os
from pathlib import Path
from typing import Sequence, Optional, Any

from telethon import TelegramClient
from telethon.tl import functions, types
from telethon.errors import RPCError


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
    """
    Создаёт новый клиент под каждый вызов.
    Это чуть медленнее, но избавляет от ошибки
    'The asyncio event loop must not change after connection'.
    """
    if not API_ID or not API_HASH:
        raise RuntimeError("TELEGRAM_API_ID/TELEGRAM_API_HASH не заданы для userbot")
    client = TelegramClient(SESSION_PATH, int(API_ID), API_HASH)
    await client.start()
    return client


def _normalize_user(user: Any) -> dict:
    """
    Accepts int|str|dict and returns dict with id/username/phone/name.
    """
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
        # if numeric str
        if user.isdigit():
            return {"id": int(user)}
        return {"username": user}
    return {"id": None}


async def _resolve_user_entity(client: TelegramClient, user: Any):
    """
    Try resolving user entity by username -> phone -> id.
    Adds contact by phone if needed to obtain access_hash.
    """
    u = _normalize_user(user)
    uid = u.get("id")
    username = u.get("username")
    phone = u.get("phone")
    display_name = u.get("name") or (f"user{uid}" if uid else "contact")

    # 1) username first (fast, works without access hash)
    if username:
        try:
            return await client.get_input_entity(f"@{username}")
        except Exception:
            pass

    # 2) phone number: import contact to get access hash
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

    # 3) raw id (works only if userbot уже видел пользователя и имеет access_hash)
    if uid:
        try:
            return await client.get_input_entity(types.PeerUser(int(uid)))
        except Exception:
            pass

    raise RuntimeError(f"Не могу получить entity для пользователя (id={uid}, username={username}, phone={phone}) — попросите его написать userbot'у или укажите username/phone")


async def create_group_chat(title: str, users: Sequence[Any]) -> dict:
    """
    Создает группу от имени userbot и добавляет указанных пользователей.
    Возвращает {chat_id, invite_link}
    """
    async with _lock:
        client = await _get_client()
        try:
            print(f"[userbot] creating group '{title}' for users={users}")
            peers = [await _resolve_user_entity(client, u) for u in users]

            # Пытаемся создать мегагруппу
            chat = None
            try:
                created = await client(functions.channels.CreateChannelRequest(
                    title=title,
                    about="",
                    megagroup=True,
                ))
                if getattr(created, "chats", None):
                    chat = created.chats[0]
                    print(f"[userbot] megagroup created id={chat.id}")
                else:
                    print(f"[userbot] CreateChannelRequest returned {type(created).__name__} without chats, fallback to CreateChatRequest")
            except RPCError as e:
                print(f"[userbot] CreateChannelRequest failed: {e}, fallback to CreateChatRequest")

            if chat is None:
                # fallback to обычный групповой чат
                created = await client(functions.messages.CreateChatRequest(
                    users=peers,
                    title=title,
                ))
                # CreateChatRequest возвращает InvitedUsers, нужный чат лежит в updates/chats
                if hasattr(created, "chats") and created.chats:
                    chat = created.chats[0]
                elif hasattr(created, "updates"):
                    updates_chats = [u.chat for u in created.updates if hasattr(u, "chat")]
                    if updates_chats:
                        chat = updates_chats[0]
                if chat is None:
                    raise RuntimeError(f"CreateChatRequest вернул {type(created).__name__} без чата")
                print(f"[userbot] basic chat created id={chat.id}")

            invited = []
            failed = []
            if peers:
                try:
                    res = await client(functions.channels.InviteToChannelRequest(
                        channel=chat,
                        users=peers,
                    ))
                    # res.users may be absent; rely on peers length
                    invited = [getattr(p, "user_id", None) or getattr(p, "id", None) for p in peers]
                    print(f"[userbot] invited users {invited}")
                except RPCError as e:
                    failed = [getattr(p, "user_id", None) or getattr(p, "id", None) for p in peers]
                    print(f"[userbot] invite failed for {failed}: {e}")
                    # даже если приглашение не удалось, ссылку всё равно вернём

            invite = await client(functions.messages.ExportChatInviteRequest(peer=chat))
            return {
                "chat_id": chat.id,
                "invite_link": invite.link,
                "invited_user_ids": invited,
                "failed_user_ids": failed,
            }
        except RPCError as e:
            raise RuntimeError(f"Telethon RPC error: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


def create_group_chat_sync(title: str, users: Sequence[Any]) -> dict | None:
    """
    Синхронная обертка для Flask.
    """
    try:
        return asyncio.run(create_group_chat(title, users))
    except Exception as e:
        print(f"⚠️ Не удалось создать чат Telegram: {e}")
        return None


async def _login_cli():
    """
    Принудительный запуск userbot для получения кода/пароля и сохранения сессии.
    """
    client = await _get_client()
    me = await client.get_me()
    print(f"✓ Userbot session готов, залогинен как @{me.username or me.first_name}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(_login_cli())
