
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, WebAppInfo

from config import BOT_TOKEN, CHANNEL_ID, ALLOWED_USER_IDS, OBSIDIAN_PATH, DOWNLOAD_DIR, KEEP_TIKTOK_FILES, DB_FILE

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()







# ----------------- Команды -----------------
@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привет!\n"
        "Бот для dance studio"
    )




@dp.message(Command("miniapp"))
async def miniapp_handler(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Open MiniApp",
                web_app=WebAppInfo(url="https://your-miniapp-url.com")
            )
        ]
    ])
    await message.answer("Открыть MiniApp", reply_markup=keyboard)

# ----------------- Main -----------------
async def main():
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
