import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dance_studio.db import ensure_db_schema, bootstrap_data
from dance_studio.core.config import BOOTSTRAP_ON_START
from dance_studio.web.app import app


def main():
    ensure_db_schema()
    if BOOTSTRAP_ON_START:
        bootstrap_data()
    app.run(host="127.0.0.1", port=3000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
