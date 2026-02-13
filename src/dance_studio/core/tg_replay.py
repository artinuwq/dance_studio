from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import logging

from sqlalchemy.exc import IntegrityError

from dance_studio.core.config import TG_INIT_REPLAY_REDIS_URL

logger = logging.getLogger(__name__)

try:
    import redis
except Exception:  # optional dependency
    redis = None


_redis_client = None


def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not TG_INIT_REPLAY_REDIS_URL or redis is None:
        return None
    try:
        _redis_client = redis.Redis.from_url(TG_INIT_REPLAY_REDIS_URL, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except (redis.RedisError, ValueError):
        logger.exception("Telegram replay Redis is unavailable, fallback to DB")
        _redis_client = None
        return None


def replay_cache_key(replay_key: str) -> str:
    return f"tg_init_used:{replay_key}"


def store_used_init_data(db, replay_key: str, ttl_seconds: int) -> bool:
    cache_key = replay_cache_key(replay_key)
    client = _get_redis_client()
    if client is not None:
        try:
            return bool(client.set(cache_key, "1", ex=ttl_seconds, nx=True))
        except redis.RedisError:
            logger.exception("Redis replay check failed, fallback to DB")

    from dance_studio.db.models import UsedInitData

    now = datetime.utcnow()
    db.query(UsedInitData).filter(UsedInitData.expires_at < now).delete(synchronize_session=False)
    record = UsedInitData(
        key_hash=hashlib.sha256(replay_key.encode("utf-8")).hexdigest(),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db.add(record)
    try:
        db.flush()
        return True
    except IntegrityError:
        db.rollback()
        return False
