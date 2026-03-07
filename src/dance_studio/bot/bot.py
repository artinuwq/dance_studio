import asyncio
import aiohttp
import time
import zipfile
import logging
import subprocess
import shutil
import hashlib
from aiogram import Bot, Dispatcher, F
from aiogram.types import MenuButtonWebApp, WebAppInfo, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaDocument
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from dance_studio.core.config import (
    API_INTERNAL_BASE_URL,
    BOT_TOKEN,
    WEB_APP_URL,
    PROJECT_NAME_FULL,
    PROJECT_NAME_SHORT,
    PROJECT_NAME_MENU,
    PROJECT_NAME_TAG,
    TECH_LOGS_CHAT_ID,
    TECH_BACKUPS_TOPIC_ID,
    TECH_STATUS_TOPIC_ID,
    TECH_CRITICAL_TOPIC_ID,
    TECH_STATUS_MESSAGE_ID,
    OWNER_IDS,
    TECH_ADMIN_ID,
    BOOKINGS_ADMIN_CHAT_ID,
)
from dance_studio.db.session import get_session
from dance_studio.core.permissions import has_permission
from dance_studio.db.models import (
    News,
    User,
    Mailing,
    Group,
    Direction,
    PaymentProfile,
    DirectionUploadSession,
    Staff,
    BookingRequest,
    Schedule,
    IndividualLesson,
    HallRental,
    GroupAbonement,
    AttendanceIntention,
    AttendanceReminder,
)
from dance_studio.core.booking_utils import format_booking_message, build_booking_keyboard_data
from dance_studio.core.tg_replay import cleanup_expired_init_data
from dance_studio.core.abonement_pricing import (
    ABONEMENT_TYPE_SINGLE,
    ABONEMENT_TYPE_MULTI,
    ABONEMENT_TYPE_TRIAL,
    is_free_trial_booking,
    parse_booking_bundle_group_ids,
)
from dance_studio.core.system_settings_service import get_setting_value, update_setting
from dance_studio.core.personal_discounts import (
    DiscountConsumptionConflictError,
    consume_one_time_discount_for_booking,
)
from dance_studio.bot.upload_sessions import direction_upload_session_validation_error
from dance_studio.bot.telegram_userbot import send_private_message
from dance_studio.core.notification_service_async import send_user_notification_async
from dance_studio.web.services.attendance import _auto_finalize_attendance_from_intentions
from dance_studio.web.services.payments import _resolve_payment_profile_payload_for_booking
from sqlalchemy import or_
from sqlalchemy.engine import make_url
from datetime import datetime, time as dt_time, timedelta
import os
import tempfile
import base64
import uuid
from pathlib import Path
from dance_studio.core.settings import DATABASE_URL

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

TECH_LOGS_CHAT_ID_RUNTIME = TECH_LOGS_CHAT_ID
TECH_BACKUPS_TOPIC_ID_RUNTIME = TECH_BACKUPS_TOPIC_ID
TECH_STATUS_TOPIC_ID_RUNTIME = TECH_STATUS_TOPIC_ID
TECH_CRITICAL_TOPIC_ID_RUNTIME = TECH_CRITICAL_TOPIC_ID
TECH_STATUS_MESSAGE_ID_RUNTIME = TECH_STATUS_MESSAGE_ID
BOOKINGS_ADMIN_CHAT_ID_RUNTIME = BOOKINGS_ADMIN_CHAT_ID

BACKUP_KEEP_COUNT = 3
BACKUP_LOCK = asyncio.Lock()
PROJECT_ROOT = Path(__file__).resolve().parents[3]
VAR_ROOT = PROJECT_ROOT / "var"
MEDIA_SOURCE_DIR = VAR_ROOT / "media"
BACKUP_DIR = VAR_ROOT / "backups"

_logger = logging.getLogger(__name__)
ATTENDANCE_REMINDER_WINDOW_HOURS = 24
ATTENDANCE_REMINDER_POLL_SECONDS = 60
ATTENDANCE_WILL_MISS_STATUS = "will_miss"
ATTENDANCE_LOCK_DELTA = timedelta(hours=2, minutes=30)
ATTENDANCE_LOCKED_MESSAGE = "Отметка закрыта. Напишите админу в случае чего-либо."
BOT_USERNAME_GLOBAL: str | None = None


PAYMENT_ADMIN_CONTACT_URL = "https://t.me/ShebaSport_LissaDance"
API_INTERNAL_BASE_URL_CLEAN = (API_INTERNAL_BASE_URL or "http://127.0.0.1:3000").strip().rstrip("/")
WEB_APP_URL_CLEAN = (WEB_APP_URL or "").strip()
INACTIVE_SCHEDULE_STATUSES = {
    "cancelled",
    "deleted",
    "rejected",
    "payment_failed",
    "CANCELLED",
    "DELETED",
    "REJECTED",
    "PAYMENT_FAILED",
}
RENTAL_CREATE_SCHEDULE_STATUSES = {"APPROVED", "AWAITING_PAYMENT", "PAID"}

TECH_LOGS_CHAT_ID_SETTING_KEY = "tech.logs_chat_id"
TECH_BACKUPS_TOPIC_ID_SETTING_KEY = "tech.backups_topic_id"
TECH_STATUS_TOPIC_ID_SETTING_KEY = "tech.status_topic_id"
TECH_CRITICAL_TOPIC_ID_SETTING_KEY = "tech.critical_topic_id"
TECH_STATUS_MESSAGE_ID_SETTING_KEY = "tech.status_message_id"
BOOKINGS_ADMIN_CHAT_ID_SETTING_KEY = "bookings.admin_chat_id"
NO_GROUPS_LAST_NOTIFIED_SETTING_KEY = "alerts.no_groups_last_notified_at"
NO_GROUPS_ALERT_COOLDOWN = timedelta(hours=6)


def _to_int_or_none(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0:
        return None
    return parsed


def _get_runtime_id_with_fallback(db, setting_key: str, env_fallback: int | None) -> int | None:
    configured = None
    try:
        configured = _to_int_or_none(get_setting_value(db, setting_key))
    except Exception:
        configured = None
    if configured is not None:
        return configured

    fallback = _to_int_or_none(env_fallback)
    if fallback is None:
        return None

    try:
        update_setting(
            db,
            key=setting_key,
            raw_value=fallback,
            changed_by_staff_id=None,
            reason="Seeded from .env fallback during runtime bootstrap",
            source="bot_runtime",
        )
    except Exception:
        _logger.exception("Failed to persist fallback for setting %s", setting_key)
    return fallback


def _persist_runtime_id_setting(setting_key: str, value: int | None, reason: str) -> None:
    normalized = _to_int_or_none(value)
    if normalized is None:
        return

    db = get_session()
    try:
        update_setting(
            db,
            key=setting_key,
            raw_value=normalized,
            changed_by_staff_id=None,
            reason=reason,
            source="bot_runtime",
        )
        db.commit()
    except Exception:
        db.rollback()
        _logger.exception("Failed to persist runtime setting %s=%s", setting_key, normalized)
    finally:
        db.close()


def _load_runtime_chat_targets() -> None:
    global TECH_LOGS_CHAT_ID_RUNTIME
    global TECH_BACKUPS_TOPIC_ID_RUNTIME
    global TECH_STATUS_TOPIC_ID_RUNTIME
    global TECH_CRITICAL_TOPIC_ID_RUNTIME
    global TECH_STATUS_MESSAGE_ID_RUNTIME
    global BOOKINGS_ADMIN_CHAT_ID_RUNTIME

    db = get_session()
    try:
        TECH_LOGS_CHAT_ID_RUNTIME = _get_runtime_id_with_fallback(
            db, TECH_LOGS_CHAT_ID_SETTING_KEY, TECH_LOGS_CHAT_ID
        )
        TECH_BACKUPS_TOPIC_ID_RUNTIME = _get_runtime_id_with_fallback(
            db, TECH_BACKUPS_TOPIC_ID_SETTING_KEY, TECH_BACKUPS_TOPIC_ID
        )
        TECH_STATUS_TOPIC_ID_RUNTIME = _get_runtime_id_with_fallback(
            db, TECH_STATUS_TOPIC_ID_SETTING_KEY, TECH_STATUS_TOPIC_ID
        )
        TECH_CRITICAL_TOPIC_ID_RUNTIME = _get_runtime_id_with_fallback(
            db, TECH_CRITICAL_TOPIC_ID_SETTING_KEY, TECH_CRITICAL_TOPIC_ID
        )
        TECH_STATUS_MESSAGE_ID_RUNTIME = _get_runtime_id_with_fallback(
            db, TECH_STATUS_MESSAGE_ID_SETTING_KEY, TECH_STATUS_MESSAGE_ID
        )
        BOOKINGS_ADMIN_CHAT_ID_RUNTIME = _get_runtime_id_with_fallback(
            db, BOOKINGS_ADMIN_CHAT_ID_SETTING_KEY, BOOKINGS_ADMIN_CHAT_ID
        )
        db.commit()
    except Exception:
        db.rollback()
        _logger.exception("Failed to load runtime chat targets from settings")
    finally:
        db.close()


def _sync_bot_username_setting(bot_username: str) -> None:
    normalized = (bot_username or "").strip().lstrip("@")
    if not normalized:
        return

    db = get_session()
    try:
        update_setting(
            db,
            key="contacts.bot_username",
            raw_value=f"@{normalized}",
            changed_by_staff_id=None,
            reason="Auto-synced from bot runtime get_me()",
            source="bot_runtime",
        )
        db.commit()
    except Exception:
        db.rollback()
        _logger.exception("Failed to sync contacts.bot_username from bot runtime")
    finally:
        db.close()


def _is_tech_admin_position(value: str | None) -> bool:
    return str(value or "").strip().lower() == "тех. админ"


async def _alert_if_groups_missing() -> None:
    db = get_session()
    try:
        groups_count = db.query(Group).count()
        if groups_count > 0:
            return

        now_utc = datetime.utcnow()
        last_sent_raw = ""
        try:
            last_sent_raw = str(get_setting_value(db, NO_GROUPS_LAST_NOTIFIED_SETTING_KEY) or "").strip()
        except Exception:
            last_sent_raw = ""

        if last_sent_raw:
            try:
                last_sent_at = datetime.fromisoformat(last_sent_raw)
                if now_utc - last_sent_at < NO_GROUPS_ALERT_COOLDOWN:
                    return
            except Exception:
                pass

        recipients: set[int] = set()
        staff_rows = db.query(Staff).filter(Staff.status == "active").all()
        for staff in staff_rows:
            if not _is_tech_admin_position(getattr(staff, "position", None)):
                continue
            telegram_id = _to_int_or_none(getattr(staff, "telegram_id", None))
            if telegram_id is not None:
                recipients.add(telegram_id)

        tech_admin_fallback = _to_int_or_none(TECH_ADMIN_ID)
        if tech_admin_fallback is not None:
            recipients.add(tech_admin_fallback)

        if not recipients:
            return

        alert_text = (
            "⚠️ ВНИМАНИЕ: в системе пока нет ни одной группы.\n"
            "Создайте группы и добавьте их в настройки."
        )

        sent_any = False
        for telegram_id in sorted(recipients):
            try:
                await bot.send_message(chat_id=telegram_id, text=alert_text)
                sent_any = True
            except Exception as exc:
                _logger.warning("Failed to send no-groups alert to %s: %s", telegram_id, exc)

        if sent_any:
            update_setting(
                db,
                key=NO_GROUPS_LAST_NOTIFIED_SETTING_KEY,
                raw_value=now_utc.isoformat(timespec="seconds"),
                changed_by_staff_id=None,
                reason="Startup alert: no groups configured",
                source="bot_runtime",
            )
            db.commit()
    except Exception:
        db.rollback()
        _logger.exception("Failed to process startup no-groups alert")
    finally:
        db.close()


async def _ensure_forum_topic(name: str, current_id: int | None, setting_key: str) -> int | None:
    if current_id:
        return current_id
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return None
    try:
        topic = await bot.create_forum_topic(chat_id=TECH_LOGS_CHAT_ID_RUNTIME, name=name)
        topic_id = topic.message_thread_id
        _persist_runtime_id_setting(
            setting_key,
            topic_id,
            reason=f"Auto-created forum topic '{name}'",
        )
        return topic_id
    except Exception as e:
        print(f"⚠️ Не удалось создать тему '{name}': {e}")
        return None

async def _ensure_topic_name(topic_id: int | None, name: str, setting_key: str | None = None) -> int | None:
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return topic_id
    if not topic_id:
        if setting_key:
            return await _ensure_forum_topic(name, None, setting_key)
        return None
    try:
        await bot.edit_forum_topic(
            chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
            message_thread_id=topic_id,
            name=name
        )
        return topic_id
    except Exception as e:
        if "message thread not found" in str(e).lower() and setting_key:
            return await _ensure_forum_topic(name, None, setting_key)
        if "TOPIC_NOT_MODIFIED" in str(e):
            return topic_id
        print(f"WARN: topic rename failed for {name}: {e}")
        return topic_id



async def ensure_tech_topics() -> None:
    global TECH_BACKUPS_TOPIC_ID_RUNTIME
    global TECH_STATUS_TOPIC_ID_RUNTIME
    global TECH_CRITICAL_TOPIC_ID_RUNTIME

    if not TECH_LOGS_CHAT_ID_RUNTIME:
        print("⚠️ TECH_LOGS_CHAT_ID не задан, темы не создаются.")
        return

    try:
        chat = await bot.get_chat(TECH_LOGS_CHAT_ID_RUNTIME)
        if not getattr(chat, "is_forum", False):
            print("⚠️ TECH_LOGS_CHAT_ID не является форум-супергруппой.")
            return
    except Exception as e:
        print(f"⚠️ Не удалось получить чат для техлогов: {e}")
        return

    TECH_BACKUPS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
        "Бэкапы", TECH_BACKUPS_TOPIC_ID_RUNTIME, TECH_BACKUPS_TOPIC_ID_SETTING_KEY
    )
    TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
        "Статус бота", TECH_STATUS_TOPIC_ID_RUNTIME, TECH_STATUS_TOPIC_ID_SETTING_KEY
    )
    TECH_CRITICAL_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
        "Критичные ошибки", TECH_CRITICAL_TOPIC_ID_RUNTIME, TECH_CRITICAL_TOPIC_ID_SETTING_KEY
    )
    TECH_BACKUPS_TOPIC_ID_RUNTIME = await _ensure_topic_name(TECH_BACKUPS_TOPIC_ID_RUNTIME, "Бэкапы", TECH_BACKUPS_TOPIC_ID_SETTING_KEY)
    TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_topic_name(TECH_STATUS_TOPIC_ID_RUNTIME, "Статус бота", TECH_STATUS_TOPIC_ID_SETTING_KEY)
    TECH_CRITICAL_TOPIC_ID_RUNTIME = await _ensure_topic_name(TECH_CRITICAL_TOPIC_ID_RUNTIME, "Критичные ошибки", TECH_CRITICAL_TOPIC_ID_SETTING_KEY)


async def _send_tech_message(
    topic_id: int | None,
    text: str,
    parse_mode: str | None = None,
    *,
    topic_name: str | None = None,
    setting_key: str | None = None,
) -> int | None:
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return topic_id
    if not topic_id:
        if topic_name and setting_key:
            topic_id = await _ensure_forum_topic(topic_name, None, setting_key)
        if not topic_id:
            return None
    try:
        await bot.send_message(
            chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
            message_thread_id=topic_id,
            text=text,
            parse_mode=parse_mode
        )
        return topic_id
    except Exception as e:
        if topic_name and setting_key and "message thread not found" in str(e).lower():
            topic_id = await _ensure_forum_topic(topic_name, None, setting_key)
            if topic_id:
                try:
                    await bot.send_message(
                        chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                        message_thread_id=topic_id,
                        text=text,
                        parse_mode=parse_mode
                    )
                    return topic_id
                except Exception:
                    pass
        print(f"⚠️ Не удалось отправить техсообщение: {e}")
        return topic_id


async def send_tech_backup(text: str) -> None:
    global TECH_BACKUPS_TOPIC_ID_RUNTIME
    TECH_BACKUPS_TOPIC_ID_RUNTIME = await _send_tech_message(
        TECH_BACKUPS_TOPIC_ID_RUNTIME,
        text,
        topic_name="Бэкапы",
        setting_key=TECH_BACKUPS_TOPIC_ID_SETTING_KEY
    )


async def send_tech_critical(text: str) -> None:
    global TECH_CRITICAL_TOPIC_ID_RUNTIME
    TECH_CRITICAL_TOPIC_ID_RUNTIME = await _send_tech_message(
        TECH_CRITICAL_TOPIC_ID_RUNTIME,
        text,
        topic_name="Критичные ошибки",
        setting_key=TECH_CRITICAL_TOPIC_ID_SETTING_KEY
    )


async def update_bot_status(text: str) -> None:
    global TECH_STATUS_MESSAGE_ID_RUNTIME
    global TECH_STATUS_TOPIC_ID_RUNTIME
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return
    if not TECH_STATUS_TOPIC_ID_RUNTIME:
        TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
            "Статус бота", None, TECH_STATUS_TOPIC_ID_SETTING_KEY
        )
        if not TECH_STATUS_TOPIC_ID_RUNTIME:
            return

    if TECH_STATUS_MESSAGE_ID_RUNTIME:
        try:
            await bot.edit_message_text(
                chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                message_id=TECH_STATUS_MESSAGE_ID_RUNTIME,
                text=text
            )
            return
        except Exception:
            TECH_STATUS_MESSAGE_ID_RUNTIME = None

    try:
        msg = await bot.send_message(
            chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
            message_thread_id=TECH_STATUS_TOPIC_ID_RUNTIME,
            text=text
        )
        TECH_STATUS_MESSAGE_ID_RUNTIME = msg.message_id
        _persist_runtime_id_setting(
            TECH_STATUS_MESSAGE_ID_SETTING_KEY,
            msg.message_id,
            reason="Stored latest status message id",
        )
    except Exception as e:
        if "message thread not found" in str(e).lower():
            TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
                "Статус бота", None, TECH_STATUS_TOPIC_ID_SETTING_KEY
            )
            if TECH_STATUS_TOPIC_ID_RUNTIME:
                try:
                    msg = await bot.send_message(
                        chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                        message_thread_id=TECH_STATUS_TOPIC_ID_RUNTIME,
                        text=text
                    )
                    TECH_STATUS_MESSAGE_ID_RUNTIME = msg.message_id
                    _persist_runtime_id_setting(
                        TECH_STATUS_MESSAGE_ID_SETTING_KEY,
                        msg.message_id,
                        reason="Stored latest status message id after topic recreation",
                    )
                    return
                except Exception:
                    pass
        print(f"⚠️ Не удалось отправить статус бота: {e}")




def _backup_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _create_db_backup_dump() -> Path:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty")

    pg_dump_path = shutil.which("pg_dump")
    if not pg_dump_path:
        raise RuntimeError("pg_dump not found in PATH")

    url = make_url(DATABASE_URL)
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError(f"Unsupported DB for pg_dump: {url.drivername}")
    if not url.database:
        raise RuntimeError("DATABASE_URL has no database name")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = BACKUP_DIR / f"db_backup_{_backup_timestamp()}.dump"

    cmd = [
        pg_dump_path,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(dump_path),
        "--dbname",
        str(url.database),
    ]
    if url.host:
        cmd.extend(["--host", str(url.host)])
    if url.port:
        cmd.extend(["--port", str(url.port)])
    if url.username:
        cmd.extend(["--username", str(url.username)])
    sslmode = (url.query or {}).get("sslmode")
    if sslmode:
        cmd.extend(["--sslmode", str(sslmode)])

    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = str(url.password)

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"pg_dump failed: {stderr or 'unknown error'}")
    return dump_path


def _create_media_backup_archive() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = BACKUP_DIR / f"media_backup_{_backup_timestamp()}.zip"

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if not MEDIA_SOURCE_DIR.exists():
            return archive_path
        for root, _, files in os.walk(MEDIA_SOURCE_DIR):
            root_path = Path(root)
            if str(root_path.resolve()).startswith(str(BACKUP_DIR.resolve())):
                continue
            for filename in files:
                file_path = root_path / filename
                arcname = file_path.relative_to(MEDIA_SOURCE_DIR)
                zf.write(file_path, arcname.as_posix())
    return archive_path


def _cleanup_old_backups() -> None:
    if not BACKUP_DIR.exists():
        return
    patterns = ["db_backup_*.dump", "media_backup_*.zip"]
    for pattern in patterns:
        backups = sorted(
            BACKUP_DIR.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old_backup in backups[BACKUP_KEEP_COUNT:]:
            try:
                old_backup.unlink()
            except Exception:
                pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _format_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


async def _ensure_backup_topic() -> int | None:
    global TECH_BACKUPS_TOPIC_ID_RUNTIME
    if not TECH_BACKUPS_TOPIC_ID_RUNTIME:
        TECH_BACKUPS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
            "Бэкапы", None, TECH_BACKUPS_TOPIC_ID_SETTING_KEY
        )
    return TECH_BACKUPS_TOPIC_ID_RUNTIME


async def create_and_send_backup(reason: str, notify_user_id: int | None = None) -> None:
    async with BACKUP_LOCK:
        try:
            db_dump_path = await asyncio.to_thread(_create_db_backup_dump)
            media_archive_path = await asyncio.to_thread(_create_media_backup_archive)
            _cleanup_old_backups()
            topic_id = await _ensure_backup_topic()
            backup_sent = False
            if topic_id:
                now_human = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                db_sha = await asyncio.to_thread(_file_sha256, db_dump_path)
                media_sha = await asyncio.to_thread(_file_sha256, media_archive_path)
                db_size = _format_size(db_dump_path.stat().st_size)
                media_size = _format_size(media_archive_path.stat().st_size)
                caption = (
                    f"📦 Backup ({reason})\n"
                    f"🗓 Date/time: {now_human}\n"
                    f"🗄 DB: {db_dump_path.name} ({db_size})\n"
                    f"🔐 DB SHA256: {db_sha}\n"
                    f"🖼 Media: {media_archive_path.name} ({media_size})\n"
                    f"🔐 Media SHA256: {media_sha}"
                )
                try:
                    await bot.send_media_group(
                        chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                        message_thread_id=topic_id,
                        media=[
                            InputMediaDocument(
                                media=FSInputFile(str(db_dump_path))
                            ),
                            InputMediaDocument(
                                media=FSInputFile(str(media_archive_path)),
                                caption=caption
                            ),
                        ],
                    )
                    backup_sent = True
                except Exception as e:
                    if "message thread not found" in str(e).lower():
                        global TECH_BACKUPS_TOPIC_ID_RUNTIME
                        TECH_BACKUPS_TOPIC_ID_RUNTIME = None
                        topic_id = await _ensure_backup_topic()
                        if topic_id:
                            await bot.send_media_group(
                                chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                                message_thread_id=topic_id,
                                media=[
                                    InputMediaDocument(
                                        media=FSInputFile(str(db_dump_path))
                                    ),
                                    InputMediaDocument(
                                        media=FSInputFile(str(media_archive_path)),
                                        caption=caption
                                    ),
                                ],
                            )
                            backup_sent = True
                    else:
                        try:
                            await bot.send_document(
                                chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                                message_thread_id=topic_id,
                                document=FSInputFile(str(db_dump_path)),
                                caption=caption
                            )
                            await bot.send_document(
                                chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                                message_thread_id=topic_id,
                                document=FSInputFile(str(media_archive_path)),
                                caption=caption
                            )
                            backup_sent = True
                        except Exception:
                            raise

            if backup_sent and reason == "scheduled" and datetime.now().hour == 12:
                def _run_cleanup_sync() -> None:
                    db = get_session()
                    try:
                        deleted = cleanup_expired_init_data(db)
                        db.commit()
                        _logger.info("used_init_data cleanup completed, deleted=%d", deleted)
                    except Exception:
                        try:
                            db.rollback()
                        except Exception:
                            _logger.exception("used_init_data cleanup rollback failed")
                        _logger.exception("used_init_data cleanup failed")
                    finally:
                        db.close()

                try:
                    await asyncio.to_thread(_run_cleanup_sync)
                except Exception:
                    _logger.exception("Cleanup after backup encountered an error")

            if notify_user_id:
                await bot.send_message(
                    chat_id=notify_user_id,
                    text="Backup (DB + media) created and sent."
                )
        except Exception as e:
            try:
                await send_tech_critical(f"Backup failed: {type(e).__name__}: {e}")
            except Exception:
                _logger.exception("Failed to send tech critical backup error")
            if notify_user_id:
                await bot.send_message(
                    chat_id=notify_user_id,
                    text="Backup failed."
                )


async def _backup_scheduler() -> None:
    while True:
        now = datetime.now()
        today = now.date()
        candidate_midnight = datetime.combine(today, dt_time(0, 0))
        candidate_noon = datetime.combine(today, dt_time(12, 0))
        if now < candidate_noon:
            next_run = candidate_midnight if now < candidate_midnight else candidate_noon
        else:
            next_run = datetime.combine(today + timedelta(days=1), dt_time(0, 0))

        sleep_seconds = max((next_run - now).total_seconds(), 1)
        await asyncio.sleep(sleep_seconds)
        await create_and_send_backup("scheduled")


async def _can_run_backup(user_id: int) -> bool:
    if user_id in OWNER_IDS or (TECH_ADMIN_ID and user_id == TECH_ADMIN_ID):
        return True
    db = get_session()
    try:
        staff = db.query(Staff).filter_by(telegram_id=user_id, status="active").first()
        if not staff or not staff.position:
            return False
        position = staff.position.strip().lower()
        return has_permission(position, "manage_backups")
    finally:
        db.close()

class CreateNewsStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_photo = State()
    waiting_for_confirmation = State()


class DirectionUploadStates(StatesGroup):
    waiting_for_session_token = State()
    waiting_for_photo = State()
    uploading_photo = State()

class StaffPhotoStates(StatesGroup):
    waiting_for_photo = State()
    uploading_photo = State()


@dp.message(CommandStart())
async def start(message, state: FSMContext):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Пользователь"
    
    # Регистрируем пользователя в БД
    await register_user_in_db(user_id, user_name, message.from_user)
    
    # Сбрасываем кастомную кнопку меню на стандартную
    try:
        await bot.set_chat_menu_button(chat_id=message.chat.id, menu_button=None)
    except Exception:
        pass

    # Получаем параметр из команды /start
    # Формат: /start параметр  или просто /start
    parts = message.text.split(maxsplit=1)
    start_param = parts[1] if len(parts) > 1 else None
    
    
    # Проверяем параметры
    if start_param == "create_news":
        # Начинаем процесс создания новости
        await message.answer(
            "✍️ <b>Создание новой новости</b>\n\n"
            "Первый шаг: введите <b>заголовок</b> новости",
            parse_mode=ParseMode.HTML
        )
        await state.set_state(CreateNewsStates.waiting_for_title)
        await state.update_data(user_id=user_id)
    
    # Проверяем, есть ли параметр для загрузки фото направления
    elif start_param and start_param.startswith("staff_photo_"):
        staff_id_str = start_param[len("staff_photo_"):]
        try:
            staff_id = int(staff_id_str)
        except ValueError:
            await message.answer("❌ Неверный формат ID сотрудника.")
            return

        db = get_session()
        try:
            staff = db.query(Staff).filter_by(id=staff_id).first()
            if not staff:
                await message.answer("❌ Сотрудник не найден.")
                return

            await state.update_data(staff_id=staff_id)
            await message.answer(
                f"📸 <b>Загрузка фото сотрудника</b>\n\n"
                f"<b>Сотрудник:</b> {staff.name}\n"
                f"Отправьте фото (JPG/PNG).",
                parse_mode=ParseMode.HTML
            )
            await state.set_state(StaffPhotoStates.waiting_for_photo)
        except Exception as e:
            print(f"Ошибка при подготовке загрузки фото сотрудника: {e}")
            await message.answer("❌ Ошибка при подготовке загрузки фото.")
        finally:
            db.close()

    elif start_param and start_param.startswith("upload_"):
        # Извлекаем токен из параметра (upload_TOKEN)
        token = start_param[7:]  # Убираем "upload_" префикс
        
        db = get_session()
        try:
            session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
            validation_error = direction_upload_session_validation_error(session, user_id)
            if validation_error:
                await message.answer(validation_error)
                return
            
            if not session:
                await message.answer(
                    "❌ Токен не найден. Проверьте, что ссылка скопирована правильно."
                )
                return
            
            if session.status != "waiting_for_photo":
                await message.answer(
                    f"❌ Сессия уже в процессе обработки (статус: {session.status})"
                )
                return
            
            # Сохраняем данные в контексте
            await state.update_data(
                session_token=token,
                session_id=session.session_id,
                user_id=user_id
            )
            
            # Сразу переходим к загрузке фотографии
            await message.answer(
                f"✅ <b>Сессия найдена!</b>\n\n"
                f"<b>Направление:</b> {session.title}\n"
                f"<b>Описание:</b> {session.description}\n"
                f"<b>Цена:</b> {session.base_price} ₽\n\n"
                f"📸 Отправьте фотографию направления (JPG, PNG):",
                parse_mode=ParseMode.HTML
            )
            
            await state.set_state(DirectionUploadStates.waiting_for_photo)
            
        except Exception as e:
            print(f"Ошибка при обработке токена: {e}")
            await message.answer("❌ Ошибка при обработке сессии")
        finally:
            db.close()
    
    else:
        # Создаем inline-кнопку для открытия приложения
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🚀 Открыть приложение",
                web_app=WebAppInfo(url=WEB_APP_URL)
            )]
        ])
        
        await message.answer(
            "<b>Добро пожаловать!</b>\n\n"
            "Записывайтесь на занятия, следите за новостями и управляйте своим профилем прямо здесь.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        print(f"DEBUG: Стандартный старт с inline-кнопкой")




@dp.message(F.contact)
async def handle_contact_share(message):
    contact = message.contact
    if not contact:
        return
    if contact.user_id and message.from_user and contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, отправьте ваш собственный номер.")
        return

    phone_number = (contact.phone_number or "").strip()
    if not phone_number:
        await message.answer("Не удалось получить номер телефона.")
        return

    db = get_session()
    try:
        user = db.query(User).filter_by(telegram_id=message.from_user.id).first()
        if not user:
            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                name=message.from_user.first_name or "Пользователь",
                phone=phone_number,
                status="active",
            )
            db.add(user)
        else:
            user.phone = phone_number
            if message.from_user.username:
                user.username = message.from_user.username
            if not user.name:
                user.name = message.from_user.first_name or "Пользователь"
        db.commit()
        await message.answer("Номер телефона сохранен.")
    except Exception:
        db.rollback()
        await message.answer("Не удалось сохранить номер. Попробуйте еще раз.")
    finally:
        db.close()


@dp.message(Command("backup"))
async def handle_backup_command(message, state: FSMContext):
    user_id = message.from_user.id
    if not await _can_run_backup(user_id):
        await message.answer("❌ Нет доступа к созданию бэкапа.")
        return
    await message.answer("⏳ Делаю бэкап и отправляю в тех. группу...")
    await create_and_send_backup("manual", notify_user_id=user_id)

async def register_user_in_db(telegram_id, name, from_user=None):
    """Регистрирует пользователя в БД если его еще нет"""
    print(f"Попытка подключения пользователь {telegram_id}")
    db = get_session()
    
    try:
        # Проверяем, существует ли пользователь
        existing_user = db.query(User).filter_by(telegram_id=telegram_id).first()
        
        if existing_user:
            print(f"✓ Пользователь {telegram_id} уже в системе")
            return
        
        # Создаем нового пользователя
        new_user = User(
            telegram_id=telegram_id,
            username=from_user.username if from_user else None,  # Получаем username из профиля Telegram
            name=name,
            phone="",  # Пусто, пользователь заполнит в профиле
            status="active"
        )
        db.add(new_user)
        db.commit()
        username_str = f"@{from_user.username}" if from_user and from_user.username else "без username"
        print(f"✅ Пользователь {telegram_id} зарегистрирован ({username_str})")
        
    except Exception as e:
        print(f"❌ Ошибка при регистрации пользователя: {e}")
        db.rollback()
    finally:
        db.close()

'''
@dp.message(Command("news"))
async def show_news(message):
    db = get_session()
    news_list = db.query(News).filter_by(status="active").order_by(News.created_at.desc()).all()
    
    if not news_list:
        await message.answer("📰 Новостей пока нет v_v")
        return
    
    text = "📰 <b>Все новости:</b>\n\n"
    
    for news in news_list:
        text += (
            f"<b>{news.title}</b>\n"
            f"<i>{news.created_at.strftime('%d.%m.%Y %H:%M')}</i>\n"
            f"{news.content}\n"
            f"{'─' * 40}\n\n"
        )
    
    await message.answer(text, parse_mode=ParseMode.HTML)
'''


# ===================== СОЗДАНИЕ НОВОСТИ =====================

@dp.message(StateFilter(CreateNewsStates.waiting_for_title))
async def handle_news_title(message, state: FSMContext):
    """Обработчик заголовка новости"""
    if message.text and len(message.text.strip()) > 0:
        await state.update_data(title=message.text.strip())
        await message.answer(
            "✍️ <b>Второй шаг:</b> введите <b>описание</b> новости",
            parse_mode=ParseMode.HTML
        )
        await state.set_state(CreateNewsStates.waiting_for_description)
    else:
        await message.answer("⚠️ Пожалуйста, введите название новости")


@dp.message(StateFilter(CreateNewsStates.waiting_for_description))
async def handle_news_description(message, state: FSMContext):
    """Обработчик описания новости"""
    if message.text and len(message.text.strip()) > 0:
        await state.update_data(description=message.text.strip())
        await message.answer(
            "📷 <b>Третий шаг:</b> отправьте фотографию (или напишите /skip для пропуска)\n\n"
            "✅ Используйте <b>квадратный формат</b> для лучшего отображения\n"
            "⚠️ Иначе фото будет обрезано автоматически из центра",
            parse_mode=ParseMode.HTML
        )
        await state.set_state(CreateNewsStates.waiting_for_photo)
    else:
        await message.answer("⚠️ Пожалуйста, введите описание новости")


@dp.message(StateFilter(CreateNewsStates.waiting_for_photo))
async def handle_news_photo(message, state: FSMContext):
    """Обработчик фотографии новости"""
    photo_data = None
    
    if message.text and message.text == "/skip":
        # Пропускаем фото
        await message.answer("⏭️ Фото пропущено")
    elif message.photo:
        # Получаем фото
        try:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            
            # Скачиваем фото используя aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
                async with session.get(url) as resp:
                    photo_bytes = await resp.read()
            
            # Конвертируем в base64
            photo_base64 = base64.b64encode(photo_bytes).decode('utf-8')
            photo_data = f"data:image/jpeg;base64,{photo_base64}"
            await state.update_data(photo_data=photo_data)
            await message.answer("✅ Фото получено")
        except Exception as e:
            await message.answer(f"❌ Ошибка при загрузке фото: {str(e)}")
            return
    else:
        await message.answer("⚠️ Отправьте фотографию или напишите /skip")
        return
    
    # Показываем превью новости
    data = await state.get_data()
    title = data.get('title', '')
    description = data.get('description', '')
    
    preview = f"<b>📰 Предпросмотр новости:</b>\n\n"
    preview += f"<b>Заголовок:</b> {title}\n\n"
    preview += f"<b>Описание:</b> {description}\n\n"
    if photo_data:
        preview += "📷 Фото прикреплено\n\n"
    preview += "Всё верно? Нажмите /confirm для публикации или /cancel для отмены"
    
    await message.answer(preview, parse_mode=ParseMode.HTML)
    await state.set_state(CreateNewsStates.waiting_for_confirmation)


@dp.message(CreateNewsStates.waiting_for_confirmation)
async def handle_news_confirmation(message, state: FSMContext):
    """Обработчик подтверждения создания новости"""
    if message.text == "/confirm":
        data = await state.get_data()
        title = data.get('title')
        description = data.get('description')
        photo_data = data.get('photo_data')
        user_id = data.get('user_id')
        
        try:
            db = get_session()
            
            # Создаем новость
            news = News(
                title=title,
                content=description,
                status="active"
            )
            db.add(news)
            db.commit()
            
            # Если есть фото, загружаем его
            if photo_data:
                try:
                    # Конвертируем base64 в файл
                    from io import BytesIO
                    import base64 as b64
                    
                    # Извлекаем base64 часть
                    base64_str = photo_data.split(',')[1] if ',' in photo_data else photo_data
                    photo_bytes = b64.b64decode(base64_str)
                    
                    # Сохраняем фото
                    from dance_studio.core.media_manager import MEDIA_DIR
                    import os
                    news_dir = os.path.join(MEDIA_DIR, "news", str(news.id))
                    os.makedirs(news_dir, exist_ok=True)
                    
                    file_path = os.path.join(news_dir, "photo.jpg")
                    with open(file_path, 'wb') as f:
                        f.write(photo_bytes)
                    
                    # Сохраняем путь в БД
                    photo_path = f"var/media/news/{news.id}/photo.jpg"
                    news.photo_path = photo_path
                    db.commit()
                except Exception as e:
                    print(f"⚠️ Ошибка при сохранении фото: {e}")
            
            await message.answer(
                "✅ <b>Новость успешно опубликована!</b>\n\n"
                "Вы можете вернуться в приложение или создать ещё одну новость (/start create_news)",
                parse_mode=ParseMode.HTML
            )
            
            db.close()
            await state.clear()
        except Exception as e:
            await message.answer(f"❌ Ошибка при создании новости: {str(e)}")
            db.close()
            await state.clear()
    
    elif message.text == "/cancel":
        await message.answer("❌ Создание новости отменено")
        await state.clear()
    else:
        await message.answer("Пожалуйста, нажмите /confirm для публикации или /cancel для отмены")


# ===================== ОТПРАВКА РАССЫЛОК =====================

# Очередь для отправки рассылок
mailing_queue = []

def queue_mailing_for_sending(mailing_id):
    """Добавляет рассылку в очередь для отправки"""
    if mailing_id not in mailing_queue:
        mailing_queue.append(mailing_id)
    #print(f"📋 Рассылка {mailing_id} добавлена в очередь отправки")

async def check_scheduled_mailings():
    """Проверяет запланированные рассылки и добавляет их в очередь если пришло время"""
    db = get_session()
    try:
        now = datetime.now()
        
        # Ищем все рассылки которые должны быть отправлены
        # scheduled_at <= текущее время и статус == 'scheduled'
        scheduled_mailings = db.query(Mailing).filter(
            Mailing.status == 'scheduled',
            Mailing.scheduled_at <= now
        ).all()
        
        for mailing in scheduled_mailings:
            if mailing.mailing_id not in mailing_queue:
                queue_mailing_for_sending(mailing.mailing_id)
                #print(f"⏰ Запланированная рассылка {mailing.mailing_id} добавлена в очередь (было время {mailing.scheduled_at})")
    except Exception as e:
        print(f"⚠️ Ошибка при проверке запланированных рассылок: {e}")
    finally:
        db.close()

async def process_mailing_queue():
    """Обрабатывает очередь рассылок"""
    while True:
        # Проверяем запланированные рассылки каждую итерацию
        await check_scheduled_mailings()
        
        if mailing_queue:
            mailing_id = mailing_queue.pop(0)
            await send_mailing_async(mailing_id)
        await asyncio.sleep(1)  # Проверяем очередь каждую секунду

async def send_mailing_async(mailing_id):
    """
    Асинхронно отправляет рассылку пользователям в зависимости от target_type:
    - user: конкретным пользователям (ID указаны в target_id через запятую)
    - group: членам группы (группа указана в target_id)
    - direction: всем пользователям направления (ID направления в target_id)
    - tg_chat: в Telegram чат (ID чата в target_id)
    - all: всем зарегистрированным пользователям
    """
    db = get_session()
    try:
        # Получаем рассылку из БД
        mailing = db.query(Mailing).filter_by(mailing_id=mailing_id).first()
        if not mailing:
            print(f"❌ Рассылка {mailing_id} не найдена")
            return False
        
        # Обновляем статус на "sending"
        mailing.status = "sending"
        db.commit()
        #print(f"📤 Начинаем отправку рассылки: {mailing.name}")
        
        # Определяем целевую аудиторию
        target_users = []
        
        if mailing.target_type == "user":
            # Отправляем конкретным пользователям
            target_id_str = str(mailing.target_id) if mailing.target_id else ""
            user_ids = [int(uid.strip()) for uid in target_id_str.split(",") if uid.strip()]
            target_users = db.query(User).filter(User.id.in_(user_ids)).all()
            
        elif mailing.target_type == "group":
            # Отправляем членам группы
            print(f"⚠️ Отправка группам пока не реализована")
            
        elif mailing.target_type == "direction":
            # Отправляем пользователям направления
            print(f"⚠️ Отправка по направлениям пока не реализована")
            
        elif mailing.target_type == "tg_chat":
            # Отправляем в Telegram чат напрямую
            chat_id = int(str(mailing.target_id)) if mailing.target_id else None
            if not chat_id:
                print(f"⚠️ Не указан ID чата для рассылки")
                mailing.status = "failed"
                db.commit()
                return False
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"<b>{mailing.name}</b>\n\n{mailing.description or mailing.purpose}",
                    parse_mode=ParseMode.HTML
                )
                #print(f"✅ Сообщение отправлено в чат {chat_id}")
                mailing.status = "sent"
                mailing.sent_at = datetime.now()
                db.commit()
                return True
            except Exception as e:
                #print(f"❌ Ошибка при отправке в чат {chat_id}: {e}")
                mailing.status = "failed"
                db.commit()
                return False
                
        elif mailing.target_type == "all":
            # Отправляем всем пользователям
            target_users = db.query(User).filter_by(status="active").all()
        
        # Отправляем сообщение каждому пользователю в целевой аудитории
        success_count = 0
        failed_count = 0
        
        for user in target_users:
            if not user.telegram_id:
                #print(f"⚠️ У пользователя {user.name} нет telegram_id")
                failed_count += 1
                continue
            
            try:
                message_text = f"<b>{mailing.name}</b>\n\n"
                if mailing.description:
                    message_text += f"{mailing.description}\n\n"
                message_text += f"<i>{mailing.purpose}</i>"
                
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=message_text,
                    parse_mode=ParseMode.HTML
                )
                success_count += 1
                #print(f"✅ Отправлено пользователю {user.name} (@{user.username})")
                
            except Exception as e:
                #print(f"❌ Ошибка при отправке пользователю {user.name}: {e}")
                failed_count += 1
                await asyncio.sleep(0.1)  # Маленькая задержка между попытками
        
        # Обновляем статус рассылки
        if success_count > 0 and failed_count == 0:
            mailing.status = "sent"
            result_text = f"успешно отправлена всем ({success_count} пользователей)"
        elif success_count > 0:
            mailing.status = "sent"
            result_text = f"отправлена частично ({success_count} успешно, {failed_count} ошибок)"
        else:
            mailing.status = "failed"
            result_text = f"не удалось отправить ({failed_count} ошибок)"
        
        mailing.sent_at = datetime.now()
        db.commit()
        #print(f"📬 Рассылка '{mailing.name}' {result_text}")
        return success_count > 0
        
    except Exception as e:
        print(f"❌ Ошибка при отправке рассылки {mailing_id}: {e}")
        try:
            mailing.status = "failed"
            db.commit()
        except:
            pass
        return False
    finally:
        db.close()

# Оставляем старую функцию для обратной совместимости
async def send_mailing(mailing_id):
    """Синхронная обёртка для отправки рассылки из Flask"""
    return await send_mailing_async(mailing_id)


def _schedule_start_dt(schedule: Schedule) -> datetime | None:
    if not schedule.date:
        return None
    start_time = schedule.time_from or schedule.start_time or dt_time(hour=12, minute=0)
    return datetime.combine(schedule.date, start_time)


def _attendance_lock_cutoff(schedule: Schedule) -> datetime | None:
    start_at = _schedule_start_dt(schedule)
    if not start_at:
        return None
    return start_at - ATTENDANCE_LOCK_DELTA


def _is_attendance_locked(schedule: Schedule) -> bool:
    cutoff = _attendance_lock_cutoff(schedule)
    if not cutoff:
        return False
    return datetime.now() >= cutoff


def _schedule_group_id(schedule: Schedule) -> int | None:
    if schedule.group_id:
        return schedule.group_id
    if schedule.object_type == "group" and schedule.object_id:
        return schedule.object_id
    return None


def _reminder_message_text(schedule: Schedule) -> str:
    date_str = schedule.date.strftime("%d.%m.%Y") if schedule.date else "—"
    tf = schedule.time_from or schedule.start_time
    tt = schedule.time_to or schedule.end_time
    time_str = "—"
    if tf and tt:
        time_str = f"{tf.strftime('%H:%M')}–{tt.strftime('%H:%M')}"
    elif tf:
        time_str = tf.strftime("%H:%M")
    title = schedule.title or "Занятие"
    return (
        f"Напоминание о занятии\n\n"
        f"{title}\n"
        f"Дата: {date_str}\n"
        f"Время: {time_str}\n\n"
        f"Если не сможете прийти, нажмите кнопку ниже."
    )


def _reminder_closed_message_text(schedule: Schedule) -> str:
    return _reminder_message_text(schedule) + "\n\nПрием отметок закрыт."


def _reminder_markup(schedule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Не приду",
                    callback_data=f"attmiss:{schedule_id}",
                )
            ]
        ]
    )


def _load_group_participants(db, schedule: Schedule) -> list[User]:
    group_id = _schedule_group_id(schedule)
    if not group_id:
        return []
    query = db.query(GroupAbonement).filter(
        GroupAbonement.group_id == group_id,
        GroupAbonement.status == "active",
    )
    if schedule.date:
        schedule_day_start = datetime.combine(schedule.date, dt_time.min)
        schedule_day_end = datetime.combine(schedule.date, dt_time.max)
        query = query.filter(
            or_(GroupAbonement.valid_from == None, GroupAbonement.valid_from <= schedule_day_end),
            or_(GroupAbonement.valid_to == None, GroupAbonement.valid_to >= schedule_day_start),
        )
    rows = query.order_by(GroupAbonement.created_at.desc()).all()
    users: list[User] = []
    seen: set[int] = set()
    for row in rows:
        if row.user_id in seen:
            continue
        seen.add(row.user_id)
        user = db.query(User).filter_by(id=row.user_id).first()
        if user:
            users.append(user)
    return users


def _load_individual_participant(db, schedule: Schedule) -> list[User]:
    if not schedule.object_id:
        return []
    lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first()
    if not lesson or not lesson.student_id:
        return []
    user = db.query(User).filter_by(id=lesson.student_id).first()
    return [user] if user else []


def _load_schedule_participants(db, schedule: Schedule) -> list[User]:
    if schedule.object_type == "group":
        return _load_group_participants(db, schedule)
    if schedule.object_type == "individual":
        return _load_individual_participant(db, schedule)
    return []


def _is_user_participant_of_schedule(db, schedule: Schedule, user_id: int) -> bool:
    users = _load_schedule_participants(db, schedule)
    return any(u.id == user_id for u in users)


async def _send_attendance_reminder_to_user(db, schedule: Schedule, user: User) -> None:
    now = datetime.now()
    row = db.query(AttendanceReminder).filter_by(schedule_id=schedule.id, user_id=user.id).first()
    if row and row.send_status in {"sent", "failed"}:
        return
    if not row:
        row = AttendanceReminder(
            schedule_id=schedule.id,
            user_id=user.id,
            send_status="pending",
        )
        db.add(row)
        db.flush()

    row.attempted_at = now
    row.send_error = None

    if not user.telegram_id:
        row.send_status = "failed"
        row.send_error = "missing_telegram_id"
        db.commit()
        return

    try:
        msg = await bot.send_message(
            chat_id=user.telegram_id,
            text=_reminder_message_text(schedule),
            reply_markup=_reminder_markup(schedule.id),
        )
        row.send_status = "sent"
        row.sent_at = now
        row.telegram_chat_id = user.telegram_id
        row.telegram_message_id = msg.message_id
        row.send_error = None
    except Exception as e:
        row.send_status = "failed"
        row.send_error = str(e)[:1000]
    db.commit()


async def send_due_attendance_reminders() -> None:
    db = get_session()
    try:
        now = datetime.now()
        future_limit = now + timedelta(hours=ATTENDANCE_REMINDER_WINDOW_HOURS)
        schedules = db.query(Schedule).filter(
            Schedule.object_type.in_(["group", "individual"]),
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
            Schedule.date.isnot(None),
        ).all()
        for schedule in schedules:
            start_at = _schedule_start_dt(schedule)
            if not start_at:
                continue
            if not (now < start_at <= future_limit):
                continue
            if _is_attendance_locked(schedule):
                continue
            users = _load_schedule_participants(db, schedule)
            for user in users:
                await _send_attendance_reminder_to_user(db, schedule, user)
    except Exception as e:
        print(f"⚠️ attendance reminder sender failed: {e}")
    finally:
        db.close()


async def close_locked_attendance_reminders() -> None:
    db = get_session()
    try:
        rows = (
            db.query(AttendanceReminder)
            .filter(
                AttendanceReminder.send_status == "sent",
                AttendanceReminder.button_closed_at == None,
            )
            .all()
        )
        now = datetime.now()
        for row in rows:
            schedule = db.query(Schedule).filter_by(id=row.schedule_id).first()
            if not schedule:
                row.button_closed_at = now
                row.send_error = "schedule_not_found_on_close"
                continue

            if not _is_attendance_locked(schedule):
                continue

            if row.telegram_chat_id and row.telegram_message_id:
                try:
                    await bot.edit_message_text(
                        chat_id=row.telegram_chat_id,
                        message_id=int(row.telegram_message_id),
                        text=_reminder_closed_message_text(schedule),
                        reply_markup=None,
                    )
                except Exception as e:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=row.telegram_chat_id,
                            message_id=int(row.telegram_message_id),
                            reply_markup=None,
                        )
                    except Exception as e2:
                        row.send_error = str(e2)[:1000]
                    else:
                        row.send_error = str(e)[:1000]

            row.button_closed_at = now
            if not row.responded_at and not row.response_action:
                row.response_action = "will_attend_auto"
                row.responded_at = now
        db.commit()
    except Exception as e:
        print(f"⚠️ attendance reminder close failed: {e}")
    finally:
        db.close()


async def finalize_closed_attendance_windows() -> None:
    db = get_session()
    try:
        today = datetime.now().date()
        schedules = db.query(Schedule).filter(
            Schedule.object_type.in_(["group", "individual"]),
            Schedule.status.notin_(list(INACTIVE_SCHEDULE_STATUSES)),
            Schedule.date.isnot(None),
            Schedule.date <= today,
        ).all()

        finalized_total = 0
        for schedule in schedules:
            finalized_total += _auto_finalize_attendance_from_intentions(db, schedule)

        if finalized_total > 0:
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"⚠️ attendance auto-finalize failed: {e}")
    finally:
        db.close()


async def process_attendance_reminders() -> None:
    while True:
        await close_locked_attendance_reminders()
        await finalize_closed_attendance_windows()
        await send_due_attendance_reminders()
        await asyncio.sleep(ATTENDANCE_REMINDER_POLL_SECONDS)


async def run_bot():
    _load_runtime_chat_targets()

    # Получаем информацию о боте при старте
    try:
        me = await bot.get_me()
        bot_username = me.username
        print(f"[bot] started: @{bot_username}")
        # Сохраняем в глобальную переменную
        global BOT_USERNAME_GLOBAL
        BOT_USERNAME_GLOBAL = bot_username
        _sync_bot_username_setting(bot_username)
    except Exception as e:
        print(f"[bot] failed to get bot info: {e}")
    
    # Запускаем обработку очереди рассылок в фоне
    backup_task = None
    queue_task = None
    reminder_task = None
    await _alert_if_groups_missing()
    await ensure_tech_topics()
    await update_bot_status(f"✅ Бот запущен {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    await create_and_send_backup("startup")
    backup_task = asyncio.create_task(_backup_scheduler())
    queue_task = asyncio.create_task(process_mailing_queue())
    reminder_task = asyncio.create_task(process_attendance_reminders())
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        try:
            await send_tech_critical(f"❌ Bot polling error: {type(e).__name__}: {e}")
        except Exception:
            pass
        raise
    finally:
        try:
            await update_bot_status(f"⛔ Бот остановлен {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        except Exception:
            pass
        if backup_task:
            backup_task.cancel()
        if queue_task:
            queue_task.cancel()
        if reminder_task:
            reminder_task.cancel()


def _build_booking_keyboard_markup(
    status: str,
    object_type: str,
    booking_id: int,
    booking: BookingRequest | None = None,
) -> InlineKeyboardMarkup | None:
    keyboard_data = build_booking_keyboard_data(
        status,
        object_type,
        booking_id,
        is_free_group_trial=is_free_trial_booking(booking) if booking else False,
    )
    if not keyboard_data:
        return None
    rows = []
    for row in keyboard_data:
        rows.append(
            [
                InlineKeyboardButton(
                    text=button["text"],
                    callback_data=button["callback_data"]
                )
                for button in row
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _create_schedule_from_lesson(
    db,
    lesson: IndividualLesson,
    status: str,
    booking: BookingRequest | None = None,
) -> Schedule | None:
    if not lesson.date or not lesson.time_from or not lesson.time_to:
        return None
    schedule = Schedule(
        object_type="individual",
        object_id=lesson.id,
        group_id=(booking.group_id if booking else None),
        date=lesson.date,
        time_from=lesson.time_from,
        time_to=lesson.time_to,
        status=status,
        title="Индивидуальное занятие",
        start_time=lesson.time_from,
        end_time=lesson.time_to,
        teacher_id=lesson.teacher_id,
    )
    db.add(schedule)
    db.flush()
    return schedule


def _normalized_booking_status(status: str | None) -> str:
    return (status or "").upper()


def _map_booking_status_to_rental_states(status: str) -> tuple[str, str, str]:
    normalized_status = _normalized_booking_status(status)
    if normalized_status == "PAID":
        return "approved", "paid", "active"
    if normalized_status in {"APPROVED", "AWAITING_PAYMENT"}:
        return "approved", "pending", "active"
    if normalized_status == "REJECTED":
        return "rejected", "rejected", "cancelled"
    if normalized_status in {"CANCELLED", "PAYMENT_FAILED"}:
        return "approved", "rejected", "cancelled"
    return "pending", "pending", "pending"


def _find_rental_for_booking(db, booking: BookingRequest) -> HallRental | None:
    if not booking.user_id:
        return None
    if not booking.date or not booking.time_from or not booking.time_to:
        return None
    return (
        db.query(HallRental)
        .filter(
            HallRental.creator_type == "user",
            HallRental.creator_id == booking.user_id,
            HallRental.date == booking.date,
            HallRental.time_from == booking.time_from,
            HallRental.time_to == booking.time_to,
        )
        .order_by(HallRental.id.desc())
        .first()
    )


def _ensure_rental_for_booking(db, booking: BookingRequest, status: str) -> HallRental | None:
    if not booking.date or not booking.time_from or not booking.time_to:
        return None

    review_status, payment_status, activity_status = _map_booking_status_to_rental_states(status)
    rental = _find_rental_for_booking(db, booking)
    if not rental:
        start_dt = datetime.combine(booking.date, booking.time_from)
        end_dt = datetime.combine(booking.date, booking.time_to)
        rental = HallRental(
            creator_id=booking.user_id or 0,
            creator_type="user",
            date=booking.date,
            time_from=booking.time_from,
            time_to=booking.time_to,
            purpose=booking.comment,
            comment=booking.comment,
            review_status=review_status,
            payment_status=payment_status,
            activity_status=activity_status,
            start_time=start_dt,
            end_time=end_dt,
            duration_minutes=booking.duration_minutes,
            status=status,
        )
        db.add(rental)
        db.flush()
        return rental

    if booking.comment and not rental.comment:
        rental.comment = booking.comment
    if booking.comment and not rental.purpose:
        rental.purpose = booking.comment
    if booking.duration_minutes and not rental.duration_minutes:
        rental.duration_minutes = booking.duration_minutes
    rental.review_status = review_status
    rental.payment_status = payment_status
    rental.activity_status = activity_status
    rental.status = status
    return rental


def _sync_rental_booking_status_to_schedule(
    db,
    booking: BookingRequest,
    staff: Staff | None,
    status: str,
) -> None:
    if not booking.date or not booking.time_from or not booking.time_to:
        return

    normalized_status = _normalized_booking_status(status)
    booking_tag = f"#{booking.id}"
    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.object_type == "rental",
            Schedule.status_comment.isnot(None),
            Schedule.status_comment.contains(booking_tag),
        )
        .order_by(Schedule.id.desc())
        .first()
    )

    rental = None
    if schedule and schedule.object_id:
        rental = db.query(HallRental).filter_by(id=schedule.object_id).first()
    if not rental:
        rental = _find_rental_for_booking(db, booking)
    if rental or normalized_status in RENTAL_CREATE_SCHEDULE_STATUSES:
        rental = _ensure_rental_for_booking(db, booking, status)

    if not schedule and rental:
        schedule = (
            db.query(Schedule)
            .filter(
                Schedule.object_type == "rental",
                Schedule.object_id == rental.id,
            )
            .order_by(Schedule.id.desc())
            .first()
        )
    if not schedule:
        schedule = (
            db.query(Schedule)
            .filter(
                Schedule.object_type == "rental",
                Schedule.date == booking.date,
                Schedule.time_from == booking.time_from,
                Schedule.time_to == booking.time_to,
            )
            .order_by(Schedule.id.desc())
            .first()
        )
        if schedule and rental and not schedule.object_id:
            schedule.object_id = rental.id

    if not schedule and normalized_status in RENTAL_CREATE_SCHEDULE_STATUSES:
        schedule = Schedule(
            object_type="rental",
            object_id=(rental.id if rental else None),
            date=booking.date,
            time_from=booking.time_from,
            time_to=booking.time_to,
            status=status,
            status_comment=f"Синхронизировано с заявкой #{booking.id}",
            title="Аренда зала",
            start_time=booking.time_from,
            end_time=booking.time_to,
        )
        db.add(schedule)
        db.flush()

    if not schedule:
        return

    schedule.date = booking.date
    schedule.time_from = booking.time_from
    schedule.time_to = booking.time_to
    schedule.start_time = booking.time_from
    schedule.end_time = booking.time_to
    if not schedule.title:
        schedule.title = "Аренда зала"
    if rental and not schedule.object_id:
        schedule.object_id = rental.id
    schedule.status = status
    schedule.status_comment = f"Синхронизировано с заявкой #{booking.id}"
    if staff:
        schedule.updated_by = staff.id


def _sync_booking_status_to_schedule(db, booking: BookingRequest, staff: Staff | None, status: str) -> None:
    if not booking.object_type:
        return
    if booking.object_type == "rental":
        _sync_rental_booking_status_to_schedule(db, booking, staff, status)
        return

    filters = [Schedule.object_type == booking.object_type]
    if booking.group_id:
        filters.append(or_(Schedule.group_id == booking.group_id, Schedule.object_id == booking.group_id))
    elif booking.teacher_id:
        filters.append(Schedule.teacher_id == booking.teacher_id)

    if booking.date:
        filters.append(Schedule.date == booking.date)
    if booking.time_from:
        filters.append(Schedule.time_from == booking.time_from)
    if booking.time_to:
        filters.append(Schedule.time_to == booking.time_to)

    if len(filters) <= 1:
        return

    schedule = (
        db.query(Schedule)
        .filter(*filters)
        .order_by(Schedule.date.desc())
        .first()
    )
    if not schedule and booking.object_type == "individual":
        lesson = (
            db.query(IndividualLesson)
            .filter_by(booking_id=booking.id)
            .first()
        )
        if lesson:
            schedule = _create_schedule_from_lesson(db, lesson, status)
    if not schedule:
        return

    schedule.status = status
    schedule.status_comment = f"Синхронизировано с заявкой #{booking.id}"
    if staff:
        schedule.updated_by = staff.id

    if schedule.object_type == "individual" and schedule.object_id:
        lesson = db.query(IndividualLesson).filter_by(id=schedule.object_id).first()
        if lesson:
            lesson.status = status
            lesson.status_updated_at = datetime.now()
            lesson.status_updated_by_id = staff.id if staff else None


def _activate_group_abonement_from_booking(db, booking: BookingRequest) -> GroupAbonement | None:
    if booking.object_type != "group":
        return None
    if not booking.user_id or not booking.group_id:
        return None

    bundle_group_ids = parse_booking_bundle_group_ids(booking)
    if not bundle_group_ids:
        bundle_group_ids = [int(booking.group_id)]
    if int(booking.group_id) not in bundle_group_ids:
        bundle_group_ids.insert(0, int(booking.group_id))
    bundle_size = max(1, min(3, len(bundle_group_ids)))
    bundle_group_ids = bundle_group_ids[:bundle_size]

    abonement_type = (booking.abonement_type or ABONEMENT_TYPE_MULTI).strip().lower()
    if abonement_type not in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_MULTI, ABONEMENT_TYPE_TRIAL}:
        abonement_type = ABONEMENT_TYPE_MULTI

    total_lessons = 0
    try:
        total_lessons = int(booking.lessons_count or 0)
    except (TypeError, ValueError):
        total_lessons = 0

    if abonement_type in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_TRIAL}:
        lessons_per_group = 1
    elif total_lessons > 0 and total_lessons % bundle_size == 0:
        lessons_per_group = max(1, total_lessons // bundle_size)
    else:
        base_group = db.query(Group).filter_by(id=int(booking.group_id)).first()
        fallback_per_week = 1
        if base_group and base_group.lessons_per_week not in (None, ""):
            try:
                fallback_per_week = max(1, int(base_group.lessons_per_week))
            except (TypeError, ValueError):
                fallback_per_week = 1
        lessons_per_group = fallback_per_week * 4

    if booking.group_start_date:
        valid_from = datetime.combine(booking.group_start_date, dt_time.min)
    else:
        valid_from = datetime.now()

    if booking.valid_until:
        valid_to = datetime.combine(booking.valid_until, dt_time.max)
    elif abonement_type in {ABONEMENT_TYPE_SINGLE, ABONEMENT_TYPE_TRIAL}:
        valid_to = datetime.combine(valid_from.date(), dt_time.max)
    else:
        valid_to = valid_from + timedelta(days=28)

    existing_rows = (
        db.query(GroupAbonement)
        .filter(
            GroupAbonement.user_id == booking.user_id,
            GroupAbonement.status == "active",
            GroupAbonement.group_id.in_(bundle_group_ids),
            GroupAbonement.abonement_type == abonement_type,
            GroupAbonement.bundle_size == bundle_size,
            GroupAbonement.valid_from == valid_from,
            GroupAbonement.valid_to == valid_to,
            GroupAbonement.balance_credits == lessons_per_group,
        )
        .all()
    )
    if len(existing_rows) == bundle_size:
        by_group_id = {row.group_id: row for row in existing_rows}
        if all(group_id in by_group_id for group_id in bundle_group_ids):
            return by_group_id[bundle_group_ids[0]]

    bundle_id = str(uuid.uuid4()) if bundle_size > 1 else None
    activated: list[GroupAbonement] = []
    for group_id in bundle_group_ids:
        pending_row = (
            db.query(GroupAbonement)
            .filter_by(user_id=booking.user_id, group_id=group_id, status="pending_activation")
            .order_by(GroupAbonement.created_at.desc())
            .first()
        )
        if pending_row:
            pending_row.status = "active"
            pending_row.balance_credits = lessons_per_group
            pending_row.valid_from = valid_from
            pending_row.valid_to = valid_to
            pending_row.abonement_type = abonement_type
            pending_row.bundle_size = bundle_size
            pending_row.bundle_id = bundle_id
            activated.append(pending_row)
            continue

        abonement = GroupAbonement(
            user_id=booking.user_id,
            group_id=group_id,
            abonement_type=abonement_type,
            bundle_id=bundle_id,
            bundle_size=bundle_size,
            balance_credits=lessons_per_group,
            status="active",
            valid_from=valid_from,
            valid_to=valid_to,
        )
        db.add(abonement)
        activated.append(abonement)

    return activated[0] if activated else None


@dp.callback_query(F.data.startswith("attmiss:"))
async def handle_attendance_absence_callback(callback: CallbackQuery):
    if not callback.data:
        return

    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    try:
        schedule_id = int(parts[1])
    except ValueError:
        await callback.answer("Некорректный идентификатор занятия", show_alert=True)
        return

    db = get_session()
    try:
        telegram_id = callback.from_user.id if callback.from_user else None
        if not telegram_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await callback.answer("Профиль пользователя не найден", show_alert=True)
            return

        schedule = db.query(Schedule).filter_by(id=schedule_id).first()
        if not schedule:
            await callback.answer("Занятие не найдено", show_alert=True)
            return
        if (schedule.status or "").lower() in INACTIVE_SCHEDULE_STATUSES:
            await callback.answer("Занятие отменено", show_alert=True)
            return
        if _is_attendance_locked(schedule):
            if callback.message:
                try:
                    await callback.message.edit_text(
                        _reminder_closed_message_text(schedule),
                        reply_markup=None,
                    )
                except Exception:
                    try:
                        await callback.message.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
            await callback.answer(ATTENDANCE_LOCKED_MESSAGE, show_alert=True)
            return
        if not _is_user_participant_of_schedule(db, schedule, user.id):
            await callback.answer("Вы не записаны на это занятие", show_alert=True)
            return

        now = datetime.now()
        intention = db.query(AttendanceIntention).filter_by(schedule_id=schedule_id, user_id=user.id).first()
        if not intention:
            intention = AttendanceIntention(
                schedule_id=schedule_id,
                user_id=user.id,
                status=ATTENDANCE_WILL_MISS_STATUS,
                source="telegram_bot",
            )
            db.add(intention)
        else:
            intention.status = ATTENDANCE_WILL_MISS_STATUS
            intention.source = "telegram_bot"

        reminder = db.query(AttendanceReminder).filter_by(schedule_id=schedule_id, user_id=user.id).first()
        if not reminder:
            reminder = AttendanceReminder(
                schedule_id=schedule_id,
                user_id=user.id,
                send_status="sent",
            )
            db.add(reminder)

        reminder.responded_at = now
        reminder.response_action = ATTENDANCE_WILL_MISS_STATUS
        reminder.button_closed_at = now
        if callback.message:
            reminder.telegram_chat_id = callback.message.chat.id
            reminder.telegram_message_id = callback.message.message_id

        db.commit()

        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

        await callback.answer("Отметили: не приду")
    finally:
        db.close()


@dp.callback_query(F.data.startswith("booking"))
async def handle_booking_action(callback: CallbackQuery):
    if not callback.data or not callback.message:
        return

    if BOOKINGS_ADMIN_CHAT_ID_RUNTIME and callback.message.chat.id != BOOKINGS_ADMIN_CHAT_ID_RUNTIME:
        await callback.answer("Эта кнопка доступна только для админ-группы.", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    if len(parts) < 2:
        await callback.answer("Некорректное действие.", show_alert=True)
        return

    prefix, booking_id_str = parts[0], parts[1]
    action = parts[2] if len(parts) == 3 else None
    try:
        booking_id = int(booking_id_str)
    except ValueError:
        await callback.answer("Некорректный идентификатор заявки.", show_alert=True)
        return

    db = get_session()
    try:
        booking = db.query(BookingRequest).filter_by(id=booking_id).first()
        if not booking:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        if prefix == "booking_cancel":
            user = db.query(User).filter_by(id=booking.user_id).first()
            text = format_booking_message(booking, user)
            reply_markup = _build_booking_keyboard_markup(booking.status, booking.object_type, booking.id, booking)
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            await callback.answer("Отмена подтверждения.")
            return

        free_trial_flow = is_free_trial_booking(booking)

        # Backward compatibility for old "approve" callbacks in admin chat:
        # treat it as "request_payment" for paid group bookings.
        if booking.object_type == "group" and action == "approve" and not free_trial_flow:
            action = "request_payment"
            if prefix == "booking_confirm":
                prefix = "booking"
        if prefix == "booking_confirm":
            if action not in {"approve", "reject"}:
                await callback.answer("Неверное подтверждение.", show_alert=True)
                return

        if prefix != "booking_confirm":
            allowed_actions = {
                button["callback_data"].split(":")[-1]
                for row in build_booking_keyboard_data(
                    booking.status,
                    booking.object_type,
                    booking.id,
                    is_free_group_trial=is_free_trial_booking(booking),
                )
                for button in row
            }
            if action not in allowed_actions:
                await callback.answer("Действие недоступно для текущего статуса.", show_alert=True)
                return
            if action in {"approve", "reject"}:
                confirm_markup = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Да",
                            callback_data=f"booking_confirm:{booking.id}:{action}"
                        ),
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=f"booking_cancel:{booking.id}"
                        ),
                    ]
                ])
                user = db.query(User).filter_by(id=booking.user_id).first()
                text = format_booking_message(booking, user)
                await callback.message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=confirm_markup
                )
                await callback.answer("Подтвердите действие повторно.")
                return

        if prefix == "booking_confirm":
            action_map = {
                "approve": "APPROVED",
                "reject": "REJECTED",
            }
        else:
            action_map = {
                "approve": "APPROVED",
                "reject": "REJECTED",
                "request_payment": "AWAITING_PAYMENT",
                "cancel": "CANCELLED",
                "confirm_payment": "PAID",
                "payment_failed": "PAYMENT_FAILED",
            }
        next_status = action_map.get(action)
        if not next_status:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return

        admin_user = callback.from_user
        staff = db.query(Staff).filter_by(telegram_id=admin_user.id, status="active").first()

        if next_status == "PAID":
            try:
                consume_one_time_discount_for_booking(db, booking=booking)
            except DiscountConsumptionConflictError:
                _logger.warning(
                    "booking %s: blocked PAID transition due to consumed one-time discount (discount_id=%s, user_id=%s)",
                    booking.id,
                    booking.applied_discount_id,
                    booking.user_id,
                )
                await callback.answer(
                    "Одноразовая скидка уже была использована в другой заявке. Оплату подтвердить нельзя.",
                    show_alert=True,
                )
                return

        booking.status = next_status
        booking.status_updated_by_id = staff.id if staff else None
        booking.status_updated_by_username = f"@{admin_user.username}" if admin_user.username else None
        booking.status_updated_by_name = staff.name if staff else admin_user.full_name
        booking.status_updated_at = datetime.now()

        _sync_booking_status_to_schedule(db, booking, staff, next_status)

        should_activate_group_abonement = (
            booking.object_type == "group"
            and (
                next_status == "PAID"
                or (next_status == "APPROVED" and free_trial_flow)
            )
        )
        if should_activate_group_abonement:
            _activate_group_abonement_from_booking(db, booking)

        db.commit()

        user = db.query(User).filter_by(id=booking.user_id).first()
        text = format_booking_message(booking, user)
        reply_markup = _build_booking_keyboard_markup(booking.status, booking.object_type, booking.id, booking)

        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
        await callback.answer("Статус заявки обновлен.")
        asyncio.create_task(_notify_user_on_status_change(user, booking, next_status))
    finally:
        db.close()


def _get_active_payment_profile(db):
    return (
        db.query(PaymentProfile)
        .filter(PaymentProfile.is_active.is_(True))
        .order_by(PaymentProfile.slot.asc())
        .first()
    ) or db.query(PaymentProfile).order_by(PaymentProfile.slot.asc()).first()


def _compute_booking_payment_amount(db, booking: BookingRequest) -> int | None:
    if booking.requested_amount is not None:
        try:
            amount = int(booking.requested_amount)
        except (TypeError, ValueError):
            return None
        return amount if amount >= 0 else None
    if booking.object_type != "group":
        return None
    if not booking.group_id or not booking.lessons_count:
        return None
    try:
        lessons_count = int(booking.lessons_count)
    except (TypeError, ValueError):
        return None
    if lessons_count <= 0:
        return None
    group = db.query(Group).filter_by(id=booking.group_id).first()
    if not group:
        return None
    direction = db.query(Direction).filter_by(direction_id=group.direction_id).first()
    if not direction or not direction.base_price:
        return None
    try:
        base_price = int(direction.base_price)
    except (TypeError, ValueError):
        return None
    if base_price <= 0:
        return None
    return lessons_count * base_price


def _build_payment_request_message(db, booking: BookingRequest) -> str:
    profile = _resolve_payment_profile_payload_for_booking(db, booking) or {}
    bank = (str(profile.get("recipient_bank") or "—")).strip() or "—"
    number = (str(profile.get("recipient_number") or "—")).strip() or "—"
    full_name = (str(profile.get("recipient_full_name") or "—")).strip() or "—"
    amount = _compute_booking_payment_amount(db, booking)
    amount_text = f"{amount:,} ₽".replace(",", " ") if amount else "уточните у администратора"

    return (
        "Здравствуйте!\n"
        f"Это администрация {PROJECT_NAME_FULL} Studio.\n\n"
        "Реквизиты для оплаты:\n"
        f"• Банк получателя: {bank}\n"
        f"• Номер: {number}\n"
        f"• ФИО получателя: {full_name}\n"
        f"• Сумма к оплате: {amount_text}\n\n"
        "Пожалуйста, после оплаты отправьте чек для подтверждения."
    )


async def _notify_payment_delivery_failed(user: User | None, booking: BookingRequest, reason: str, failed_text: str) -> None:
    if not BOOKINGS_ADMIN_CHAT_ID_RUNTIME:
        return
    user_label = "неизвестный пользователь"
    if user:
        username = f"@{user.username}" if user.username else "—"
        user_label = f"{user.name} (id={user.telegram_id or '—'}, username={username})"
    elif booking.user_telegram_id:
        user_label = f"id={booking.user_telegram_id}"
    reason_text = (reason or "неизвестная ошибка").strip()

    alert = (
        "⚠️ Не получилось отправить сообщение пользователю.\n"
        f"Получатель: {user_label}\n"
        f"Причина: {reason_text}\n\n"
        "По возможности отправьте сообщение вручную."
    )
    try:
        await bot.send_message(chat_id=BOOKINGS_ADMIN_CHAT_ID_RUNTIME, text=alert)
        await bot.send_message(chat_id=BOOKINGS_ADMIN_CHAT_ID_RUNTIME, text=failed_text)
    except Exception:
        pass


async def _send_payment_message_from_admin_account(user: User | None, booking: BookingRequest) -> None:
    telegram_id = user.telegram_id if user else booking.user_telegram_id
    if not telegram_id:
        return

    local_db = get_session()
    try:
        payment_text = _build_payment_request_message(local_db, booking)
    finally:
        local_db.close()
    user_target = {
        "id": telegram_id,
        "username": user.username if user else None,
        "phone": user.phone if user else None,
        "name": user.name if user else None,
    }

    try:
        await asyncio.wait_for(send_private_message(user_target, payment_text), timeout=15)
    except Exception as exc:
        reason = str(exc).strip() or "неизвестная ошибка"
        if isinstance(exc, asyncio.TimeoutError):
            reason = "таймаут отправки сообщения от userbot (15 сек)"
        await _notify_payment_delivery_failed(user, booking, reason, payment_text)
        try:
            fallback_text = (
                "Не получилось отправить реквизиты для оплаты.\n"
                f"Пожалуйста, добавьте админский аккаунт в контакты: {PAYMENT_ADMIN_CONTACT_URL}"
            )
            await send_user_notification_async(
                bot=bot,
                user_id=telegram_id,
                text=fallback_text,
                context_note="Ошибка отправки реквизитов (userbot fail)"
            )
        except Exception:
            pass


async def _notify_user_on_status_change(user: User | None, booking: BookingRequest, status: str) -> None:
    telegram_id = user.telegram_id if user else booking.user_telegram_id
    if not telegram_id:
        return

    should_send_payment_details = (
        status == "AWAITING_PAYMENT"
        or (status == "APPROVED" and booking.object_type in {"rental", "individual"})
    )
    if should_send_payment_details:
        await _send_payment_message_from_admin_account(user, booking)
        return

    text_map = {
        "APPROVED": "Ваша заявка подтверждена. В ближайшее время с вами свяжется администратор для обсуждения оплаты.",
        "REJECTED": "К сожалению, вашу заявку отклонили. При необходимости вы можете отправить новую заявку или обратиться к администратору.",
        "PAID": "Ваша заявка полностью одобрена, ждем вас на занятиях!",
    }
    message_text = text_map.get(status)
    if not message_text:
        return

    if status == "PAID":
        user_target = {
            "id": telegram_id,
            "username": user.username if user else None,
            "phone": user.phone if user else None,
            "name": user.name if user else None,
        }
        try:
            await asyncio.wait_for(send_private_message(user_target, message_text), timeout=15)
        except Exception as exc:
            reason = str(exc).strip() or "неизвестная ошибка"
            if isinstance(exc, asyncio.TimeoutError):
                reason = "таймаут отправки сообщения от userbot (15 сек)"
            await _notify_payment_delivery_failed(user, booking, reason, message_text)
        return

    try:
        await send_user_notification_async(
            bot=bot,
            user_id=telegram_id,
            text=message_text,
            context_note=f"Смена статуса заявки: {status}"
        )
    except Exception:
        pass


# ======================== СИСТЕМА ЗАГРУЗКИ ФОТОГРАФИЙ НАПРАВЛЕНИЙ ========================

@dp.message(Command("upload_direction"))
async def start_direction_upload(message, state: FSMContext):
    """Начинает процесс загрузки фотографии для направления"""
    user_id = message.from_user.id
    
    # Регистрируем пользователя если его нет
    await register_user_in_db(user_id, message.from_user.first_name, message.from_user)
    
    # Проверяем, что это администратор
    db = get_session()
    try:
        from dance_studio.db.models import Staff
        admin = db.query(Staff).filter_by(telegram_id=user_id).first()
        
        if not admin or admin.position not in ["администратор", "владелец", "тех. админ"]:
            await message.answer(
                "❌ У вас нет прав администратора для создания направлений."
            )
            return
        
    finally:
        db.close()
    
    await message.answer(
        "📸 <b>Загрузка фотографии для направления</b>\n\n"
        "Введите <b>токен сессии</b>, который вы получили на сайте:\n\n"
        "(Это нужно для связи с направлением, которое вы создаете)",
        parse_mode=ParseMode.HTML
    )
    
    await state.set_state(DirectionUploadStates.waiting_for_session_token)


@dp.message(DirectionUploadStates.waiting_for_session_token)
async def process_session_token(message, state: FSMContext):
    """Получает токен сессии и проверяет его"""
    token = message.text.strip()
    
    db = get_session()
    try:
        session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
        validation_error = direction_upload_session_validation_error(
            session,
            message.from_user.id,
        )
        if validation_error:
            await message.answer(validation_error)
            return
        
        if not session:
            await message.answer(
                "❌ Токен не найден. Проверьте, что вы скопировали его правильно."
            )
            return
        
        if session.status != "waiting_for_photo":
            await message.answer(
                f"❌ Сессия уже в процессе обработки (статус: {session.status})"
            )
            return
        
        # Сохраняем данные в контексте
        await state.update_data(
            session_token=token,
            session_id=session.session_id,
            user_id=message.from_user.id
        )
        
        await message.answer(
            f"✅ Сессия найдена!\n\n"
            f"<b>Направление:</b> {session.title}\n"
            f"<b>Описание:</b> {session.description}\n"
            f"<b>Цена:</b> {session.base_price} ₽\n\n"
            f"Отправьте фотографию направления (JPG, PNG):",
            parse_mode=ParseMode.HTML
        )
        
        await state.set_state(DirectionUploadStates.waiting_for_photo)
        
    finally:
        db.close()

# TO DO: ВЫРЕЗАТЬ К ХУЯМ
@dp.message(DirectionUploadStates.waiting_for_photo)
async def process_direction_photo(message, state: FSMContext):
    """Получает фотографию и загружает её на сервер"""
    if not message.photo:
        await message.answer("❌ Пожалуйста, отправьте фотографию")
        return
    
    await state.set_state(DirectionUploadStates.uploading_photo)
    await message.answer("⏳ Загружаю фотографию на сервер...")
    
    try:
        # Получаем данные из контекста
        data = await state.get_data()
        token = data.get("session_token")
        session_id = data.get("session_id")
        
        # Скачиваем фотографию с Telegram
        file_info = await bot.get_file(message.photo[-1].file_id)
        
        # Скачиваем файл
        file_path = await bot.download_file(file_info.file_path)
        
        # Читаем содержимое файла
        file_content = file_path.read()
        
        # Загружаем на сервер через API
        try:
            # Используем aiohttp для загрузки
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field('photo', file_content, filename=f'photo_{session_id}.jpg', content_type='image/jpeg')
                
                async with session.post(
                    f"{API_INTERNAL_BASE_URL_CLEAN}/api/directions/photo/{token}",
                    data=form
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        
                        # Создаем кнопку для возврата к веб-приложению
                        keyboard = None
                        if WEB_APP_URL_CLEAN:
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🩰 Вернуться на сайт",
                                    web_app=WebAppInfo(url=WEB_APP_URL_CLEAN)
                                )]
                            ])
                        
                        # Отправляем сообщение об успехе с кнопкой возврата
                        await message.answer(
                            f"✅ <b>Фотография успешно загружена!</b>\n\n"
                            f"Нажмите кнопку ниже, чтобы вернуться на сайт и завершить создание направления.",
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard
                        )
                        
                        # Очищаем состояние
                        await state.clear()
                        return
                    else:
                        error_msg = await resp.text()
                        raise Exception(f"Ошибка сервера: {resp.status} - {error_msg}")
        
        except Exception as e:
            print(f"❌ Ошибка при загрузке на сервер: {e}")
            await message.answer(
                f"❌ Ошибка при загрузке фотографии на сервер:\n{str(e)}\n\n"
                f"Попробуйте снова, отправив фотографию:"
            )
            await state.set_state(DirectionUploadStates.waiting_for_photo)
    
    except Exception as e:
        print(f"❌ Ошибка при обработке фотографии: {e}")
        await message.answer(
            "❌ Ошибка при обработке фотографии. Попробуйте еще раз."
        )
        await state.set_state(DirectionUploadStates.waiting_for_photo)


@dp.message(StaffPhotoStates.waiting_for_photo)
async def process_staff_photo(message, state: FSMContext):
    if not message.photo:
        await message.answer("❌ Пожалуйста, отправьте фото (JPG/PNG).")
        return

    await state.set_state(StaffPhotoStates.uploading_photo)
    data = await state.get_data()
    staff_id = data.get("staff_id")
    if not staff_id:
        await message.answer("❌ ID сотрудника не найден.")
        await state.clear()
        return

    try:
        file_info = await bot.get_file(message.photo[-1].file_id)
        file_path = await bot.download_file(file_info.file_path)
        file_content = file_path.read()

        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field('photo', file_content, filename=f'photo_{staff_id}.jpg', content_type='image/jpeg')

            async with session.post(
                f"{API_INTERNAL_BASE_URL_CLEAN}/staff/{staff_id}/photo",
                data=form
            ) as resp:
                if resp.status in (200, 201):
                    await message.answer("✅ Фото сотрудника успешно загружено.")
                    await state.clear()
                    return

                error_msg = await resp.text()
                raise Exception(f"Ошибка сервера: {resp.status} - {error_msg}")

    except Exception as e:
        print(f"❌ Ошибка при загрузке фото сотрудника: {e}")
        await message.answer(
            f"❌ Ошибка при загрузке фото:\n{str(e)}\n\n"
            f"Попробуйте отправить фото еще раз."
        )
        await state.set_state(StaffPhotoStates.waiting_for_photo)
