from __future__ import annotations

from flask import Flask, request


_CSP_POLICY = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "form-action 'self'",
        (
            "script-src 'self' 'unsafe-inline' "
            "https://telegram.org https://*.telegram.org https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru "
            "https://api-maps.yandex.ru https://yandex.ru https://*.yandex.ru "
            "https://yandex.net https://*.yandex.net https://yastatic.net https://*.yastatic.net"
        ),
        (
            "style-src 'self' 'unsafe-inline' "
            "https://yandex.ru https://*.yandex.ru "
            "https://yandex.net https://*.yandex.net https://yastatic.net https://*.yastatic.net"
        ),
        (
            "img-src 'self' data: blob: "
            "https://telegram.org https://*.telegram.org https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru "
            "https://yandex.ru https://*.yandex.ru "
            "https://yandex.net https://*.yandex.net https://yastatic.net https://*.yastatic.net"
        ),
        (
            "connect-src 'self' https://api.vk.com https://*.vk.com "
            "https://telegram.org https://*.telegram.org https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru "
            "https://api-maps.yandex.ru https://yandex.ru https://*.yandex.ru "
            "https://yandex.net https://*.yandex.net https://yastatic.net https://*.yastatic.net"
        ),
        "font-src 'self' data: https://yastatic.net https://*.yastatic.net",
        (
            "frame-src 'self' https://*.telegram.org https://*.vk.com https://vk.com "
            "https://yandex.ru https://*.yandex.ru https://yandex.net https://*.yandex.net"
        ),
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org https://*.vk.com https://vk.com https://*.vk.ru https://vk.ru",
    ]
)


def _set_security_headers(response):
    # Keep compatibility with current frontend (inline script/style and third-party scripts).
    response.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")

    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    return response


def register_security_headers_middleware(app: Flask) -> None:
    app.after_request(_set_security_headers)
