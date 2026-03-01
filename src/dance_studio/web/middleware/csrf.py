from flask import Flask


def register_csrf_middleware(app: Flask) -> None:
    # CSRF в проекте проверяется в middleware.auth.before_request.
    # Этот модуль оставлен как явная точка архитектуры для будущего выделения
    # CSRF-проверки в отдельный before_request handler.
    app.config.setdefault("CSRF_MIDDLEWARE_ENABLED", True)
    app.config.setdefault("CSRF_VALIDATION_LOCATION", "auth.before_request")
