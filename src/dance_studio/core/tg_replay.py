from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import logging

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from dance_studio.db.models import UsedInitData

logger = logging.getLogger(__name__)


def cleanup_expired_init_data(db, *, now: datetime | None = None, batch_size: int = 1000) -> int:
    current_time = now or datetime.utcnow()
    total_deleted = 0

    while True:
        ids = [
            row[0]
            for row in db.execute(
                select(UsedInitData.id)
                .where(UsedInitData.expires_at < current_time)
                .limit(batch_size)
            ).fetchall()
        ]
        if not ids:
            break

        db.execute(delete(UsedInitData).where(UsedInitData.id.in_(ids)))
        db.commit()
        total_deleted += len(ids)

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
