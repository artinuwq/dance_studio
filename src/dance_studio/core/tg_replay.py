from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import logging

from sqlalchemy.exc import IntegrityError

from dance_studio.db.models import UsedInitData

logger = logging.getLogger(__name__)


def cleanup_expired_init_data(db, *, now: datetime | None = None) -> int:
    current_time = now or datetime.utcnow()
    deleted = db.query(UsedInitData).filter(UsedInitData.expires_at < current_time).delete(synchronize_session=False)
    return int(deleted or 0)


def store_used_init_data(db, replay_key: str, ttl_seconds: int) -> bool:
    record = UsedInitData(
        key_hash=hashlib.sha256(replay_key.encode("utf-8")).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds),
    )

    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
        return True
    except IntegrityError:
        logger.info("Telegram initData replay detected")
        return False
