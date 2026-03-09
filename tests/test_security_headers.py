from flask import Flask, jsonify

from dance_studio.web.middleware.security import register_security_headers_middleware


def _build_app() -> Flask:
    app = Flask(__name__)
    register_security_headers_middleware(app)

    @app.get("/ok")
    def ok():
        return jsonify({"ok": True})

    @app.get("/custom-csp")
    def custom_csp():
        response = jsonify({"ok": True})
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        return response

    return app


def test_security_headers_present_for_http_response():
    app = _build_app()

    response = app.test_client().get("/ok")

    assert response.status_code == 200
    assert response.headers.get("Content-Security-Policy")
    assert "frame-ancestors" in response.headers["Content-Security-Policy"]
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Permissions-Policy") == "camera=(), microphone=(), geolocation=()"
    assert response.headers.get("Strict-Transport-Security") is None


def test_hsts_is_added_for_https():
    app = _build_app()

    response = app.test_client().get("/ok", base_url="https://example.test")

    assert response.status_code == 200
    assert response.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"


def test_existing_csp_header_is_not_overwritten():
    app = _build_app()

    response = app.test_client().get("/custom-csp")

    assert response.status_code == 200
    assert response.headers.get("Content-Security-Policy") == "default-src 'none'"
