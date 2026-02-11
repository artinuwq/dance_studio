import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

# Отдельный токен для пользовательского бота
USER_BOT_TOKEN = (os.getenv("USER_BOT_TOKEN") or os.getenv("BOT_TOKEN_USER") or "").strip()

# Лёгкий диспетчер только для пользовательского бота
dp_user = Dispatcher()


@dp_user.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("Привет! Пользовательский бот на связи.")


async def run_user_bot():
    if not USER_BOT_TOKEN:
        print("⚠️ USER_BOT_TOKEN не задан — пользовательский бот не запущен")
        return
    if " " in USER_BOT_TOKEN:
        print("⚠️ USER_BOT_TOKEN содержит пробелы — пропускаем запуск user-bot")
        return
    bot = Bot(token=USER_BOT_TOKEN)
    print("✓ Запускаем пользовательский бот")
    await dp_user.start_polling(bot)


__all__ = ["run_user_bot"]
