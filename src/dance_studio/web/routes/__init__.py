from .admin import bp as admin_bp
from .attendance import bp as attendance_bp
from .auth import bp as auth_bp
from .bookings import bp as bookings_bp
from .media import bp as media_bp
from .news import bp as news_bp
from .payments import bp as payments_bp

__all__ = [
    "admin_bp",
    "attendance_bp",
    "auth_bp",
    "bookings_bp",
    "media_bp",
    "news_bp",
    "payments_bp",
]
