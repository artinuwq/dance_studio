from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from dance_studio.db.models import UsedInitData

logger = logging.getLogger(__name__)


def cleanup_expired_init_data(db, *, now: datetime | None = None, batch_size: int = 1000) -> int:
    current_time = now or datetime.utcnow()
    total_deleted = 0
    delete_sql = text(
        "DELETE FROM used_init_data "
        "WHERE id IN (SELECT id FROM used_init_data WHERE expires_at < :now LIMIT :batch) "
        "RETURNING id"
    )
    while True:
        res = db.execute(delete_sql, {"now": current_time, "batch": batch_size})
        deleted_rows = len(res.fetchall())
        if deleted_rows == 0:
            break
        total_deleted += deleted_rows
        db.commit()
    return total_deleted


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
