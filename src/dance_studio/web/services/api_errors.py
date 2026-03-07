from __future__ import annotations

import hashlib

from flask import current_app

INTERNAL_SERVER_ERROR_CODE = "internal_server_error"


def client_error_response(message: str, status: int = 400):
    return {"error": message}, status


def safe_client_error_message(exc: Exception, fallback: str = "invalid_request") -> str:
    message = str(exc).strip()
    if isinstance(exc, KeyError) and len(message) >= 2 and message[0] == message[-1] and message[0] in {"'", '"'}:
        message = message[1:-1]
    return message or fallback


def internal_server_error_response(
    *,
    context: str,
    db=None,
    public_message: str = INTERNAL_SERVER_ERROR_CODE,
):
    if db is not None:
        try:
            db.rollback()
        except Exception:
            current_app.logger.exception("%s: rollback failed", context)
    current_app.logger.exception(context)
    return client_error_response(public_message, 500)


def token_fingerprint(token: str | None, *, length: int = 12) -> str:
    if not token:
        return "missing"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:length]
