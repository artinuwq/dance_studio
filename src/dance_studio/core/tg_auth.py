import hashlib
import hmac
import json
import os
from urllib.parse import parse_qsl

from dance_studio.core.config import BOT_TOKEN


def validate_init_data(init_data: str):
    token = BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("tg_auth: BOT_TOKEN not set", flush=True)
        return None
    if not init_data:
        print("tg_auth: empty init_data", flush=True)
        return None

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got_hash = data.pop("hash", "")
    if not got_hash:
        print("tg_auth: missing hash", init_data, flush=True)
        return None

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    print("tg_auth: data_check_string", data_check_string, flush=True)

    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    print("tg_auth: got hash", got_hash, flush=True)
    print("tg_auth: calc hash", calc_hash, flush=True)

    if not hmac.compare_digest(calc_hash, got_hash):
        print("tg_auth: hash mismatch", flush=True)
        return None

    user_json = data.get("user")
    if not user_json:
        print("tg_auth: user payload missing", flush=True)
        return None
    try:
        return json.loads(user_json)
    except json.JSONDecodeError as e:
        print("tg_auth: user json decode error", e, flush=True)
        return None


__all__ = ["validate_init_data"]
