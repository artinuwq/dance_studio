from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from dance_studio.core.config import APP_SECRET_KEY
from dance_studio.web.constants import MAX_UPLOAD_MB
from dance_studio.web.middleware import (
    register_auth_middleware,
    register_csrf_middleware,
    register_error_handlers,
)
from dance_studio.web.routes import (
    admin_bp,
    attendance_bp,
    auth_bp,
    bookings_bp,
    media_bp,
    news_bp,
    payments_bp,
)


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    app.secret_key = APP_SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    app.config["MAX_FORM_MEMORY_SIZE"] = MAX_UPLOAD_MB * 1024 * 1024

    register_auth_middleware(app)
    register_csrf_middleware(app)
    register_error_handlers(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(bookings_bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(news_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(admin_bp)

    return app


app = create_app()

__all__ = ["app", "create_app"]
