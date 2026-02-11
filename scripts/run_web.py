import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dance_studio.web.app import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=3000, debug=False, use_reloader=False)
