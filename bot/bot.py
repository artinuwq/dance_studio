import asyncio
import aiohttp
import time
from aiogram import Bot, Dispatcher
from aiogram.types import MenuButtonWebApp, WebAppInfo, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from config import BOT_TOKEN
from backend.db import get_session
from backend.models import News, User, Mailing, Group, DirectionUploadSession
from datetime import datetime
import os
import tempfile
import base64

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


class CreateNewsStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_photo = State()
    waiting_for_confirmation = State()


class DirectionUploadStates(StatesGroup):
    waiting_for_session_token = State()
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
                url="https://lumica.duckdns.org/"
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
    queue_task = asyncio.create_task(process_mailing_queue())
    
    try:
        await dp.start_polling(bot)
    finally:
        queue_task.cancel()


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


