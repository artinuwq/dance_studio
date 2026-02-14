import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from dance_studio.core.config import BOT_TOKEN, TG_INIT_DATA_MAX_AGE_SECONDS


@dataclass(slots=True)
class InitDataValidationResult:
    user_id: int
    username: str | None
    first_name: str | None
    replay_key: str


def validate_init_data(init_data: str) -> InitDataValidationResult | None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not configured")

    if not init_data:
        return None

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got_hash = data.get("hash")
    if not got_hash:
        return None

    payload = dict(data)
    payload.pop("hash", None)

    data_check_string = "\n".join(f"{k}={payload[k]}" for k in sorted(payload.keys()))

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    calc_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calc_hash, got_hash):
        return None

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None

    if abs(time.time() - auth_date) > TG_INIT_DATA_MAX_AGE_SECONDS:
        return None

    user_json = data.get("user")
    if not user_json:
        return None

    try:
        user = json.loads(user_json)
    except json.JSONDecodeError:
        return None

    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError):
        return None

    query_id = data.get("query_id", "").strip()
    replay_key = query_id or got_hash  # replay_key MUST NOT be logged

    return InitDataValidationResult(
        user_id=user_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        replay_key=replay_key,
    )
