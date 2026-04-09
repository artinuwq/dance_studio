from __future__ import annotations

from flask import Blueprint, current_app, request

bp = Blueprint("security_routes", __name__)


@bp.route("/csp-report", methods=["POST"])
def csp_report():
    payload = request.get_json(silent=True)
    if payload is None:
        raw = request.get_data(cache=False, as_text=True)
        if raw:
            current_app.logger.info("csp-report raw=%s", raw[:2048])
        else:
            current_app.logger.info("csp-report empty")
    else:
        current_app.logger.info("csp-report payload=%s", payload)
    return "", 204


__all__ = ["bp"]
