import asyncio
import aiohttp
import time
import zipfile
from aiogram import Bot, Dispatcher, F
from aiogram.types import MenuButtonWebApp, WebAppInfo, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from config import (
    BOT_TOKEN,
    WEB_APP_URL,
    TECH_LOGS_CHAT_ID,
    TECH_BACKUPS_TOPIC_ID,
    TECH_STATUS_TOPIC_ID,
    TECH_CRITICAL_TOPIC_ID,
    TECH_STATUS_MESSAGE_ID,
    OWNER_IDS,
    TECH_ADMIN_ID,
    BOOKINGS_ADMIN_CHAT_ID,
)
from backend.db import get_session
from backend.permissions import has_permission
from backend.models import News, User, Mailing, Group, DirectionUploadSession, Staff, BookingRequest, Schedule, IndividualLesson
from backend.booking_utils import format_booking_message, build_booking_keyboard_data
from sqlalchemy import or_
from datetime import datetime, time as dt_time, timedelta
import os
import tempfile
import base64
from pathlib import Path

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

TECH_LOGS_CHAT_ID_RUNTIME = TECH_LOGS_CHAT_ID
TECH_BACKUPS_TOPIC_ID_RUNTIME = TECH_BACKUPS_TOPIC_ID
TECH_STATUS_TOPIC_ID_RUNTIME = TECH_STATUS_TOPIC_ID
TECH_CRITICAL_TOPIC_ID_RUNTIME = TECH_CRITICAL_TOPIC_ID
TECH_STATUS_MESSAGE_ID_RUNTIME = TECH_STATUS_MESSAGE_ID

BACKUP_KEEP_COUNT = 3
BACKUP_LOCK = asyncio.Lock()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SOURCE_DIR = PROJECT_ROOT / "database"
BACKUP_DIR = BACKUP_SOURCE_DIR / "backups"


def _env_file_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _upsert_env_value(key: str, value: int) -> None:
    if value is None:
        return
    env_path = _env_file_path()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        existing_key = line.split("=", 1)[0].strip()
        if existing_key == key:
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


async def _ensure_forum_topic(name: str, current_id: int | None, env_key: str) -> int | None:
    if current_id:
        return current_id
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return None
    try:
        topic = await bot.create_forum_topic(chat_id=TECH_LOGS_CHAT_ID_RUNTIME, name=name)
        topic_id = topic.message_thread_id
        _upsert_env_value(env_key, topic_id)
        return topic_id
    except Exception as e:
        print(f"⚠️ Не удалось создать тему '{name}': {e}")
        return None

async def _ensure_topic_name(topic_id: int | None, name: str, env_key: str | None = None) -> int | None:
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return topic_id
    if not topic_id:
        if env_key:
            return await _ensure_forum_topic(name, None, env_key)
        return None
    try:
        await bot.edit_forum_topic(
            chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
            message_thread_id=topic_id,
            name=name
        )
        return topic_id
    except Exception as e:
        if "message thread not found" in str(e).lower() and env_key:
            return await _ensure_forum_topic(name, None, env_key)
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
        "Бэкапы", TECH_BACKUPS_TOPIC_ID_RUNTIME, "TECH_BACKUPS_TOPIC_ID"
    )
    TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
        "Статус бота", TECH_STATUS_TOPIC_ID_RUNTIME, "TECH_STATUS_TOPIC_ID"
    )
    TECH_CRITICAL_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
        "Критичные ошибки", TECH_CRITICAL_TOPIC_ID_RUNTIME, "TECH_CRITICAL_TOPIC_ID"
    )
    TECH_BACKUPS_TOPIC_ID_RUNTIME = await _ensure_topic_name(TECH_BACKUPS_TOPIC_ID_RUNTIME, "Бэкапы", "TECH_BACKUPS_TOPIC_ID")
    TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_topic_name(TECH_STATUS_TOPIC_ID_RUNTIME, "Статус бота", "TECH_STATUS_TOPIC_ID")
    TECH_CRITICAL_TOPIC_ID_RUNTIME = await _ensure_topic_name(TECH_CRITICAL_TOPIC_ID_RUNTIME, "Критичные ошибки", "TECH_CRITICAL_TOPIC_ID")


async def _send_tech_message(
    topic_id: int | None,
    text: str,
    parse_mode: str | None = None,
    *,
    topic_name: str | None = None,
    env_key: str | None = None,
) -> int | None:
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return topic_id
    if not topic_id:
        if topic_name and env_key:
            topic_id = await _ensure_forum_topic(topic_name, None, env_key)
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
        if topic_name and env_key and "message thread not found" in str(e).lower():
            topic_id = await _ensure_forum_topic(topic_name, None, env_key)
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
        env_key="TECH_BACKUPS_TOPIC_ID"
    )


async def send_tech_critical(text: str) -> None:
    global TECH_CRITICAL_TOPIC_ID_RUNTIME
    TECH_CRITICAL_TOPIC_ID_RUNTIME = await _send_tech_message(
        TECH_CRITICAL_TOPIC_ID_RUNTIME,
        text,
        topic_name="Критичные ошибки",
        env_key="TECH_CRITICAL_TOPIC_ID"
    )


async def update_bot_status(text: str) -> None:
    global TECH_STATUS_MESSAGE_ID_RUNTIME
    global TECH_STATUS_TOPIC_ID_RUNTIME
    if not TECH_LOGS_CHAT_ID_RUNTIME:
        return
    if not TECH_STATUS_TOPIC_ID_RUNTIME:
        TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
            "Статус бота", None, "TECH_STATUS_TOPIC_ID"
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
        _upsert_env_value("TECH_STATUS_MESSAGE_ID", msg.message_id)
    except Exception as e:
        if "message thread not found" in str(e).lower():
            TECH_STATUS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
                "Статус бота", None, "TECH_STATUS_TOPIC_ID"
            )
            if TECH_STATUS_TOPIC_ID_RUNTIME:
                try:
                    msg = await bot.send_message(
                        chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                        message_thread_id=TECH_STATUS_TOPIC_ID_RUNTIME,
                        text=text
                    )
                    TECH_STATUS_MESSAGE_ID_RUNTIME = msg.message_id
                    _upsert_env_value("TECH_STATUS_MESSAGE_ID", msg.message_id)
                    return
                except Exception:
                    pass
        print(f"⚠️ Не удалось отправить статус бота: {e}")




def _create_backup_archive() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = BACKUP_DIR / f"database_backup_{timestamp}.zip"

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(BACKUP_SOURCE_DIR):
            root_path = Path(root)
            if str(root_path.resolve()).startswith(str(BACKUP_DIR.resolve())):
                continue
            for filename in files:
                file_path = root_path / filename
                arcname = file_path.relative_to(BACKUP_SOURCE_DIR)
                zf.write(file_path, arcname.as_posix())

    return archive_path


def _cleanup_old_backups() -> None:
    if not BACKUP_DIR.exists():
        return
    backups = sorted(
        BACKUP_DIR.glob("database_backup_*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    for old_backup in backups[BACKUP_KEEP_COUNT:]:
        try:
            old_backup.unlink()
        except Exception:
            pass


async def _ensure_backup_topic() -> int | None:
    global TECH_BACKUPS_TOPIC_ID_RUNTIME
    if not TECH_BACKUPS_TOPIC_ID_RUNTIME:
        TECH_BACKUPS_TOPIC_ID_RUNTIME = await _ensure_forum_topic(
            "Бэкапы", None, "TECH_BACKUPS_TOPIC_ID"
        )
    return TECH_BACKUPS_TOPIC_ID_RUNTIME


async def create_and_send_backup(reason: str, notify_user_id: int | None = None) -> None:
    async with BACKUP_LOCK:
        try:
            archive_path = await asyncio.to_thread(_create_backup_archive)
            _cleanup_old_backups()
            topic_id = await _ensure_backup_topic()
            if topic_id:
                caption = f"🗄 Бэкап ({reason}) {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
                try:
                    await bot.send_document(
                        chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                        message_thread_id=topic_id,
                        document=FSInputFile(str(archive_path)),
                        caption=caption
                    )
                except Exception as e:
                    if "message thread not found" in str(e).lower():
                        global TECH_BACKUPS_TOPIC_ID_RUNTIME
                        TECH_BACKUPS_TOPIC_ID_RUNTIME = None
                        topic_id = await _ensure_backup_topic()
                        if topic_id:
                            await bot.send_document(
                                chat_id=TECH_LOGS_CHAT_ID_RUNTIME,
                                message_thread_id=topic_id,
                                document=FSInputFile(str(archive_path)),
                                caption=caption
                            )
            if notify_user_id:
                await bot.send_message(
                    chat_id=notify_user_id,
                    text="✅ Бэкап создан и отправлен в супергруппу."
                )
        except Exception as e:
            try:
                await send_tech_critical(f"? ?????? ??????: {type(e).__name__}: {e}")
            except Exception:
                pass
            if notify_user_id:
                await bot.send_message(
                    chat_id=notify_user_id,
                    text="❌ Не удалось создать бэкап."
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
    
    #TODO: ВОТ ЭТО НАДО ПОМЕНЯТЬ
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="🩰 LISSA DANCE",
            web_app=WebAppInfo(
                url=(WEB_APP_URL or "https://lumica.duckdns.org/")
            )
        )
    )

    # Получаем параметр из команды /start
    # Формат: /start параметр  или просто /start
    parts = message.text.split(maxsplit=1)
    start_param = parts[1] if len(parts) > 1 else None
    
    print(f"DEBUG: start_param = {start_param}")  # Для отладки
    
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
        
        print(f"DEBUG: token = {token}")  # Для отладки
        
        db = get_session()
        try:
            session = db.query(DirectionUploadSession).filter_by(session_token=token).first()
            
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
        await message.answer(
            "Добро пожаловать!\n\n"
            "Приложение доступно через кнопку внизу чата 👇"
        )
        print(f"DEBUG: Стандартный старт без параметров")




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
                    from backend.media_manager import MEDIA_DIR
                    import os
                    news_dir = os.path.join(MEDIA_DIR, "news", str(news.id))
                    os.makedirs(news_dir, exist_ok=True)
                    
                    file_path = os.path.join(news_dir, "photo.jpg")
                    with open(file_path, 'wb') as f:
                        f.write(photo_bytes)
                    
                    # Сохраняем путь в БД
                    photo_path = f"database/media/news/{news.id}/photo.jpg"
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
        # scheduled_at <= текущее время И статус == 'scheduled'
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


async def run_bot():
    # Получаем информацию о боте при старте
    try:
        me = await bot.get_me()
        bot_username = me.username
        print(f"✅ Бот запущен: @{bot_username}")
        # Сохраняем в глобальную переменную
        global BOT_USERNAME_GLOBAL
        BOT_USERNAME_GLOBAL = bot_username
    except Exception as e:
        print(f"⚠️ Не удалось получить информацию о боте: {e}")
    
    # Запускаем обработку очереди рассылок в фоне
    backup_task = None
    queue_task = None
    await ensure_tech_topics()
    await update_bot_status(f"✅ Бот запущен {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    await create_and_send_backup("startup")
    backup_task = asyncio.create_task(_backup_scheduler())
    queue_task = asyncio.create_task(process_mailing_queue())
    
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


def _build_booking_keyboard_markup(status: str, object_type: str, booking_id: int) -> InlineKeyboardMarkup | None:
    keyboard_data = build_booking_keyboard_data(status, object_type, booking_id)
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


def _sync_booking_status_to_schedule(db, booking: BookingRequest, staff: Staff | None, status: str) -> None:
    if not booking.object_type:
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


@dp.callback_query(F.data.startswith("booking"))
async def handle_booking_action(callback: CallbackQuery):
    if not callback.data or not callback.message:
        return

    if BOOKINGS_ADMIN_CHAT_ID and callback.message.chat.id != BOOKINGS_ADMIN_CHAT_ID:
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
            reply_markup = _build_booking_keyboard_markup(booking.status, booking.object_type, booking.id)
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            await callback.answer("Отмена подтверждения.")
            return

        if prefix == "booking_confirm":
            if action not in {"approve", "reject"}:
                await callback.answer("Неверное подтверждение.", show_alert=True)
                return

        if prefix != "booking_confirm":
            allowed_actions = {
                button["callback_data"].split(":")[-1]
                for row in build_booking_keyboard_data(booking.status, booking.object_type, booking.id)
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

        booking.status = next_status
        booking.status_updated_by_id = staff.id if staff else None
        booking.status_updated_by_username = f"@{admin_user.username}" if admin_user.username else None
        booking.status_updated_by_name = staff.name if staff else admin_user.full_name
        booking.status_updated_at = datetime.now()

        _sync_booking_status_to_schedule(db, booking, staff, next_status)

        db.commit()

        user = db.query(User).filter_by(id=booking.user_id).first()
        text = format_booking_message(booking, user)
        reply_markup = _build_booking_keyboard_markup(booking.status, booking.object_type, booking.id)

        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
        await callback.answer("Статус заявки обновлен.")
        await _notify_user_on_status_change(user, booking, next_status)
    finally:
        db.close()


async def _notify_user_on_status_change(user: User | None, booking: BookingRequest, status: str) -> None:
    telegram_id = user.telegram_id if user else booking.user_telegram_id
    if not telegram_id:
        return

    text_map = {
        "APPROVED": "Ваша заявка подтверждена. В ближайшее время с вами свяжется администратор для обсуждения оплаты.",
        "REJECTED": "К сожалению, вашу заявку отклонили. При необходимости вы можете отправить новую заявку или обратиться к администратору.",
        "PAID": "Ваша заявка полностью одобрена, ждём вас на занятиях!",
    }
    message_text = text_map.get(status)
    if not message_text:
        return

    try:
        await bot.send_message(chat_id=telegram_id, text=message_text)
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
        from backend.models import Staff
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
            # Использвуем aiohttp для загрузки
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field('photo', file_content, filename=f'photo_{session_id}.jpg', content_type='image/jpeg')
                
                async with session.post(
                    f"http://localhost:5000/api/directions/photo/{token}",
                    data=form
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        
                        # Создаем кнопку для возврата к веб-приложению
                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🩰 Вернуться на сайт",
                                web_app=WebAppInfo(url="https://lumica.duckdns.org/")
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
                f"http://localhost:5000/staff/{staff_id}/photo",
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
