import asyncio
from aiogram import Bot, Dispatcher
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.filters import CommandStart
import random
from config import BOT_TOKEN

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

APP_VERSION = 3.4
@dp.message(CommandStart())
async def start(message):
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="🩰 Студия танцев",
            web_app=WebAppInfo(
                url=f"https://artinuwq.github.io/dance_studio/?{APP_VERSION}"
            )
        )
    )

    await message.answer(
        "Добро пожаловать!\n\n"
        "Приложение доступно через кнопку внизу чата 👇"
    )

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
