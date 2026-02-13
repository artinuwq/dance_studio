import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dance_studio.db.session import get_session
from dance_studio.core.tg_replay import cleanup_expired_init_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    db = get_session()
    try:
        deleted_count = cleanup_expired_init_data(db)
        db.commit()
        logger.info("used_init_data cleanup completed, deleted=%s", deleted_count)
    except Exception:
        db.rollback()
        logger.exception("used_init_data cleanup failed")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
