import asyncio
import threading
import logging

from backend.app import app
from bot.bot import run_bot

logging.basicConfig(level=logging.INFO)

def run_flask():
    try:
        app.run(host="127.0.0.1", 
                port=3000, 
                debug=False, 
                use_reloader=False)
        
    except Exception:
        logging.exception("Flask crashed")

async def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    try:
        await run_bot()
    except Exception:
        logging.exception("Bot crashed")
        raise

if __name__ == "__main__":
    asyncio.run(main())
