import json
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


def _parse_str_list(value: str, default: list[str]) -> list[str]:
    if not value:
        return default
    parts = re.split(r"[,\s]+", value.strip())
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = part.strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result or default


def _parse_int(value: str, default: int | None) -> int | None:
    if value is None or value == '':
        return default
    try:
        return int(value)
    except ValueError:
        return default


_INITIAL_STAFF_ROLE_ALIASES = {
    "тех. админ": "тех. админ",
    "тех админ": "тех. админ",
    "tech_admin": "тех. админ",
    "tech admin": "тех. админ",
    "technical admin": "тех. админ",
    "владелец": "владелец",
    "owner": "владелец",
    "старший админ": "старший админ",
    "старший_admin": "старший админ",
    "senior admin": "старший админ",
    "senior_admin": "старший админ",
    "администратор": "администратор",
    "admin": "администратор",
    "модератор": "модератор",
    "moderator": "модератор",
    "учитель": "учитель",
    "teacher": "учитель",
}


def _normalize_initial_staff_role(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower()).replace("ё", "е")
    return _INITIAL_STAFF_ROLE_ALIASES.get(normalized, "")


def _resolve_initial_staff_config_path(value: str) -> Path | None:
    raw = (value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _ROOT / path
    return path


def _load_initial_staff_assignments(path_value: str) -> list[dict]:
    config_path = _resolve_initial_staff_config_path(path_value)
    if config_path is None:
        return []
    if not config_path.exists():
        raise RuntimeError(f"INITIAL_STAFF_CONFIG_PATH points to missing file: {config_path}")

    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in initial staff config: {config_path}") from exc

    items = raw_payload.get("staff") if isinstance(raw_payload, dict) else raw_payload
    if not isinstance(items, list):
        raise RuntimeError("Initial staff config must be a JSON array or an object with a 'staff' array")

    result: list[dict] = []
    seen_telegram_ids: set[int] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Initial staff item #{index} must be an object")

        telegram_id = _parse_int(str(item.get("telegram_id", "")).strip(), None)
        if telegram_id is None or telegram_id <= 0:
            raise RuntimeError(f"Initial staff item #{index} must contain a positive telegram_id")
        if telegram_id in seen_telegram_ids:
            raise RuntimeError(f"Duplicate telegram_id in initial staff config: {telegram_id}")

        position = _normalize_initial_staff_role(str(item.get("position", "")))
        if not position:
            raise RuntimeError(
                f"Initial staff item #{index} has unsupported position: {item.get('position')!r}"
            )

        seen_telegram_ids.add(telegram_id)
        result.append({
            "telegram_id": telegram_id,
            "position": position,
            "name": str(item.get("name") or "").strip(),
            "status": str(item.get("status") or "active").strip() or "active",
        })

    return result


_ROOT = Path(__file__).resolve().parents[3]
_load_dotenv(_ROOT / '.env')

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
WEB_APP_URL = os.getenv('WEB_APP_URL', '')
API_INTERNAL_BASE_URL = os.getenv('API_INTERNAL_BASE_URL', 'http://127.0.0.1:3000').strip().rstrip('/')

DATABASE_URL = os.getenv('DATABASE_URL')
DATABASE_POOL_SIZE = max(1, _parse_int(os.getenv('DATABASE_POOL_SIZE', '3'), 3) or 3)
DATABASE_MAX_OVERFLOW = max(0, _parse_int(os.getenv('DATABASE_MAX_OVERFLOW', '2'), 2) or 2)
DATABASE_POOL_TIMEOUT_SECONDS = max(1, _parse_int(os.getenv('DATABASE_POOL_TIMEOUT_SECONDS', '30'), 30) or 30)
DATABASE_POOL_RECYCLE_SECONDS = max(30, _parse_int(os.getenv('DATABASE_POOL_RECYCLE_SECONDS', '1800'), 1800) or 1800)
ENV = os.getenv('ENV', 'dev').strip().lower()

_migrate_default = '1' if ENV == 'dev' else '0'
MIGRATE_ON_START = _parse_bool(os.getenv('MIGRATE_ON_START', _migrate_default), ENV == 'dev')
BOOTSTRAP_ON_START = _parse_bool(os.getenv('BOOTSTRAP_ON_START', '0'), False)
SESSION_TTL_DAYS = _parse_int(os.getenv('SESSION_TTL_DAYS', '60'), 60) or 60
MAX_SESSIONS_PER_USER = _parse_int(os.getenv('MAX_SESSIONS_PER_USER', '5'), 5) or 5
ROTATE_IF_DAYS_LEFT = _parse_int(os.getenv('ROTATE_IF_DAYS_LEFT', '7'), 7) or 7
TG_INIT_DATA_MAX_AGE_SECONDS = _parse_int(os.getenv('TG_INIT_DATA_MAX_AGE_SECONDS', '600'), 600) or 600
SESSION_REAUTH_IDLE_SECONDS = _parse_int(os.getenv('SESSION_REAUTH_IDLE_SECONDS', '86400'), 86400) or 86400
BACKUP_ENCRYPTION_REQUIRED = _parse_bool(os.getenv('BACKUP_ENCRYPTION_REQUIRED', '1'), True)
BACKUP_AGE_RECIPIENTS = _parse_str_list(
    os.getenv('BACKUP_AGE_RECIPIENTS', '') or os.getenv('BACKUP_AGE_RECIPIENT', ''),
    []
)
BACKUP_AGE_BINARY = (os.getenv('BACKUP_AGE_BINARY', '') or '').strip()
BACKUP_TELEGRAM_PROXY = (os.getenv('BACKUP_TELEGRAM_PROXY', '') or '').strip()
TELEGRAM_PROXY = (os.getenv('TELEGRAM_PROXY', '') or '').strip()

VK_MINI_APP_SERVICE_KEY = (os.getenv('VK_MINI_APP_SERVICE_KEY', '') or '').strip()
VK_MINI_APP_APP_ID = (os.getenv('VK_MINI_APP_APP_ID', '') or '').strip()
VK_MINI_APP_SECRET_KEY = (os.getenv('VK_MINI_APP_SECRET_KEY', '') or '').strip()
VK_COMMUNITY_ID = (os.getenv('VK_COMMUNITY_ID', '') or '').strip()
VK_COMMUNITY_ACCESS_TOKEN = (os.getenv('VK_COMMUNITY_ACCESS_TOKEN', '') or '').strip()
VK_API_VERSION = (os.getenv('VK_API_VERSION', '5.199') or '5.199').strip()
WEB_PUSH_PUBLIC_KEY = (os.getenv('WEB_PUSH_PUBLIC_KEY', '') or '').strip()
WEB_PUSH_PRIVATE_KEY = (os.getenv('WEB_PUSH_PRIVATE_KEY', '') or '').strip()
WEB_PUSH_SUBJECT = (os.getenv('WEB_PUSH_SUBJECT', 'mailto:admin@example.com') or '').strip()

APP_SECRET_KEY = os.getenv('APP_SECRET_KEY')
if not APP_SECRET_KEY:
    raise RuntimeError('APP_SECRET_KEY environment variable is required')

SESSION_PEPPER = os.getenv('SESSION_PEPPER') or APP_SECRET_KEY
COOKIE_SECURE = _parse_bool(os.getenv('COOKIE_SECURE', '1' if ENV != 'dev' else '0'), ENV != 'dev')
COOKIE_SAMESITE = os.getenv('COOKIE_SAMESITE', 'None' if COOKIE_SECURE else 'Lax')
CSRF_TRUSTED_ORIGINS = os.getenv('CSRF_TRUSTED_ORIGINS', '')

INITIAL_STAFF_CONFIG_PATH = (os.getenv('INITIAL_STAFF_CONFIG_PATH', '') or '').strip()
INITIAL_STAFF_ASSIGNMENTS = _load_initial_staff_assignments(INITIAL_STAFF_CONFIG_PATH)
_INITIAL_STAFF_OWNER_IDS = [
    item["telegram_id"]
    for item in INITIAL_STAFF_ASSIGNMENTS
    if item.get("position") == "владелец"
]
_INITIAL_STAFF_TECH_ADMIN_IDS = [
    item["telegram_id"]
    for item in INITIAL_STAFF_ASSIGNMENTS
    if item.get("position") == "тех. админ"
]

OWNER_IDS = list(_INITIAL_STAFF_OWNER_IDS)
TECH_ADMIN_ID = _INITIAL_STAFF_TECH_ADMIN_IDS[0] if _INITIAL_STAFF_TECH_ADMIN_IDS else None
BETA_TEST_MODE = _parse_bool(os.getenv('BETA_TEST_MODE', ''), True)

# Tech logs / forum topics
TECH_LOGS_CHAT_ID = _parse_int(os.getenv('TECH_LOGS_CHAT_ID', ''), None)
TECH_BACKUPS_TOPIC_ID = _parse_int(os.getenv('TECH_BACKUPS_TOPIC_ID', ''), None)
TECH_STATUS_TOPIC_ID = _parse_int(os.getenv('TECH_STATUS_TOPIC_ID', ''), None)
TECH_CRITICAL_TOPIC_ID = _parse_int(os.getenv('TECH_CRITICAL_TOPIC_ID', ''), None)
TECH_STATUS_MESSAGE_ID = _parse_int(os.getenv('TECH_STATUS_MESSAGE_ID', ''), None)
TECH_ABONEMENTS_TOPIC_ID = _parse_int(os.getenv('TECH_ABONEMENTS_TOPIC_ID', ''), None)
TECH_NOTIFICATIONS_TOPIC_ID = _parse_int(os.getenv('TECH_NOTIFICATIONS_TOPIC_ID', ''), None)
BOOKINGS_ADMIN_CHAT_ID = _parse_int(os.getenv('BOOKINGS_ADMIN_CHAT_ID', ''), None)
