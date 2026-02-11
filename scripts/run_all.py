import asyncio
import threading
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dance_studio.web.app import app
from dance_studio.bot.bot import run_bot
from dance_studio.db import ensure_schema_dev, bootstrap_data
from dance_studio.core.config import BOOTSTRAP_ON_START

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
    ensure_schema_dev()
    if BOOTSTRAP_ON_START:
        bootstrap_data()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    try:
        await run_bot()
    except Exception:
        logging.exception("Bot crashed")
        raise


if __name__ == "__main__":
    asyncio.run(main())
