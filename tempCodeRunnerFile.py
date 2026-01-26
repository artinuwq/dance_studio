import asyncio
import threading

from backend.app import app
from bot.bot import run_bot



def run_flask():
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        use_reloader=False
    )


async def main():
    # Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Telegram бот в async
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())