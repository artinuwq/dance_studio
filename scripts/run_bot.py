import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dance_studio.bot.bot import run_bot
from dance_studio.db import ensure_schema_dev, bootstrap_data
from dance_studio.core.config import BOOTSTRAP_ON_START


def main():
    ensure_schema_dev()
    if BOOTSTRAP_ON_START:
        bootstrap_data()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
