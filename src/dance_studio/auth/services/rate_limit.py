from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from dance_studio.core.time import utcnow
from threading import Lock


_LIMITS = {
    "otp_request": (3, timedelta(minutes=5)),
    "otp_verify": (6, timedelta(minutes=10)),
    "vk_login": (20, timedelta(minutes=5)),
    "telegram_login": (20, timedelta(minutes=5)),
    "passkey_login": (20, timedelta(minutes=5)),
}
_BUCKETS: dict[str, deque[datetime]] = defaultdict(deque)
_LOCK = Lock()


class RateLimitExceededError(RuntimeError):
    pass



def hit_rate_limit(action: str, subject: str) -> None:
    limit, period = _LIMITS[action]
    now = utcnow()
    bucket_key = f"{action}:{subject}"
    with _LOCK:
        bucket = _BUCKETS[bucket_key]
        while bucket and now - bucket[0] > period:
            bucket.popleft()
        if len(bucket) >= limit:
            raise RateLimitExceededError(action)
        bucket.append(now)

