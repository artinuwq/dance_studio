import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qsl

from dance_studio.core.config import BOT_TOKEN

logger = logging.getLogger(__name__)


def validate_init_data(init_data: str):
    token = BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.warning("tg_auth: BOT_TOKEN not set")
        return None
    if not init_data:
        logger.info("tg_auth: empty init_data")
        return None

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got_hash = data.pop("hash", "")
    if not got_hash:
        logger.info("tg_auth: missing hash")
        return None

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))

    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, got_hash):
        logger.warning("tg_auth: hash mismatch")
        return None

    user_json = data.get("user")
    if not user_json:
        logger.info("tg_auth: user payload missing")
        return None
    try:
        return json.loads(user_json)
    except json.JSONDecodeError:
        logger.warning("tg_auth: user json decode error")
        return None


__all__ = ["validate_init_data"]
