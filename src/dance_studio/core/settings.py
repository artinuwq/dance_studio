import os
import re
from pathlib import Path


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    val = value.strip().lower()
    if val in {'1', 'true', 'yes', 'y', 'on'}:
        return True
    if val in {'0', 'false', 'no', 'n', 'off'}:
        return False
    return default


def _parse_int_list(value: str, default: list[int]) -> list[int]:
    if not value:
        return default
    parts = re.split(r"[,\s]+", value.strip())
    result = []
    for part in parts:
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result or default


def _parse_int(value: str, default: int | None) -> int | None:
    if value is None or value == '':
        return default
    try:
        return int(value)
    except ValueError:
        return default


_ROOT = Path(__file__).resolve().parents[3]
_load_dotenv(_ROOT / '.env')

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'dance_studio_admin_bot')
WEB_APP_URL = os.getenv('WEB_APP_URL', '')

DATABASE_URL = os.getenv('DATABASE_URL')
ENV = os.getenv('ENV', 'dev').strip().lower()
AUTO_CREATE_SCHEMA = _parse_bool(os.getenv('AUTO_CREATE_SCHEMA', '0'), False)
BOOTSTRAP_ON_START = _parse_bool(os.getenv('BOOTSTRAP_ON_START', '0'), False)

APP_SECRET_KEY = os.getenv('APP_SECRET_KEY')
if not APP_SECRET_KEY:
    raise RuntimeError('APP_SECRET_KEY environment variable is required')

OWNER_IDS = _parse_int_list(os.getenv('OWNER_IDS', ''), [])
TECH_ADMIN_ID = _parse_int(os.getenv('TECH_ADMIN_ID', ''), None)
BETA_TEST_MODE = _parse_bool(os.getenv('BETA_TEST_MODE', ''), True)

# Tech logs / forum topics
TECH_LOGS_CHAT_ID = _parse_int(os.getenv('TECH_LOGS_CHAT_ID', ''), None)
TECH_BACKUPS_TOPIC_ID = _parse_int(os.getenv('TECH_BACKUPS_TOPIC_ID', ''), None)
TECH_STATUS_TOPIC_ID = _parse_int(os.getenv('TECH_STATUS_TOPIC_ID', ''), None)
TECH_CRITICAL_TOPIC_ID = _parse_int(os.getenv('TECH_CRITICAL_TOPIC_ID', ''), None)
TECH_STATUS_MESSAGE_ID = _parse_int(os.getenv('TECH_STATUS_MESSAGE_ID', ''), None)
TECH_ABONEMENTS_TOPIC_ID = _parse_int(os.getenv('TECH_ABONEMENTS_TOPIC_ID', ''), None)
BOOKINGS_ADMIN_CHAT_ID = _parse_int(os.getenv('BOOKINGS_ADMIN_CHAT_ID', ''), None)
BOOKING_ADMIN_TOPIC_ID = _parse_int(os.getenv('BOOKING_ADMIN_TOPIC_ID', ''), None)
