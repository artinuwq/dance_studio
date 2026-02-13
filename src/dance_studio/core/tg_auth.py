import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from dance_studio.core.config import BOT_TOKEN


def validate_init_data(init_data: str):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not configured")

    if not init_data:
        return None

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got_hash = data.pop("hash", None)
    if not got_hash:
        return None

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))

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

    auth_date = int(data.get("auth_date", 0))
    if abs(time.time() - auth_date) > 86400:
        return None

    user_json = data.get("user")
    if not user_json:
        return None

    try:
        user = json.loads(user_json)
    except json.JSONDecodeError:
        return None

    if "id" not in user:
        return None

    return user
