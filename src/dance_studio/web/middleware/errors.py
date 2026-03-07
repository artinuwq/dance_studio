import requests
from flask import Flask, current_app, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from dance_studio.core.tech_notifier import send_critical_sync
from dance_studio.web.services.api_errors import INTERNAL_SERVER_ERROR_CODE


def handle_unhandled_exception(error):
    if isinstance(error, HTTPException):
        return error

    try:
        send_critical_sync(f"Flask error: {type(error).__name__}: {error}")
    except (RuntimeError, ValueError, requests.RequestException):
        current_app.logger.exception("Failed to send critical error notification")

    current_app.logger.exception(
        "Unhandled exception: method=%s path=%s",
        request.method,
        request.path,
    )
    return jsonify({"error": INTERNAL_SERVER_ERROR_CODE}), 500


def handle_file_too_large(error):
    max_mb = (current_app.config.get("MAX_CONTENT_LENGTH") or 0) // (1024 * 1024) or 200
    current_app.logger.warning(
        "upload too large: content_length=%s max_mb=%s path=%s",
        request.content_length,
        max_mb,
        request.path,
    )
    return (
        jsonify({"error": "Файл слишком большой", "max_mb": max_mb}),
        413,
    )


def register_error_handlers(app: Flask) -> None:
    app.register_error_handler(Exception, handle_unhandled_exception)
    app.register_error_handler(RequestEntityTooLarge, handle_file_too_large)
