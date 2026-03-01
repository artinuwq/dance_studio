from __future__ import annotations

from datetime import timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = str(PROJECT_ROOT / "frontend")
BASE_DIR = str(Path(__file__).resolve().parent)
VAR_ROOT = PROJECT_ROOT / "var"
MEDIA_ROOT = VAR_ROOT / "media"

MAX_UPLOAD_MB = 200

ALLOWED_DIRECTION_TYPES = {"dance", "sport"}
INACTIVE_SCHEDULE_STATUSES = {
    "cancelled",
    "deleted",
    "rejected",
    "payment_failed",
    "CANCELLED",
    "DELETED",
    "REJECTED",
    "PAYMENT_FAILED",
}

ATTENDANCE_ALLOWED_STATUSES = {"present", "absent", "late", "sick"}
ATTENDANCE_INTENTION_STATUS_WILL_MISS = "will_miss"
ATTENDANCE_INTENTION_LOCK_DELTA = timedelta(hours=2, minutes=30)
ATTENDANCE_INTENTION_LOCKED_MESSAGE = (
    "Прием отметок закрыт. Напишите админу в случае чего-либо."
)
ATTENDANCE_MARKING_WINDOW_HOURS = 2

