from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from sqlalchemy.exc import IntegrityError

from dance_studio.db.models import UsedInitData


def cleanup_expired_init_data(db) -> None:
    now = datetime.utcnow()
    db.query(UsedInitData).filter(UsedInitData.expires_at < now).delete(synchronize_session=False)


def store_used_init_data(db, replay_key: str, ttl_seconds: int) -> bool:
    cleanup_expired_init_data(db)

    record = UsedInitData(
        key_hash=hashlib.sha256(replay_key.encode("utf-8")).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(record)

    try:
        db.flush()
        return True
    except IntegrityError:
        db.rollback()
        return False
