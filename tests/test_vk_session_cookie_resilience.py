from __future__ import annotations

import base64
import hashlib
import hmac
import os
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.db.models import Base
from dance_studio.web.app import create_app


@pytest.fixture(scope="module")
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def app(session_factory, monkeypatch):
    def _get_session():
        return session_factory()

    monkeypatch.setattr(auth_middleware, "get_session", _get_session)
    monkeypatch.setattr(db_module, "get_session", _get_session)
    monkeypatch.setattr(auth_middleware, "_is_csrf_valid", lambda: True)
    return create_app()


def _vk_payload(**overrides):
    payload = {
        "vk_ts": "1710000000",
        "vk_user_id": "vk-cookie-check",
        "vk_access_token_settings": "",
        "name": "VK Cookie Check",
    }
    payload.update(overrides)
    signed_part = {key: value for key, value in payload.items() if str(key).startswith("vk_")}
    params = urlencode(sorted(signed_part.items()), doseq=True)
    digest = hmac.new(b"test-secret", params.encode("utf-8"), hashlib.sha256).digest()
    payload["sign"] = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return payload


def _sid_set_cookie_headers(response) -> list[str]:
    return [header for header in response.headers.getlist("Set-Cookie") if header.lower().startswith("sid=")]


def test_vk_login_with_stale_sid_does_not_clear_fresh_session_cookie(app, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    client = app.test_client()
    client.set_cookie("sid", "stale-session")

    response = client.post("/auth/vk", json=_vk_payload())

    assert response.status_code == 200
    sid_headers = _sid_set_cookie_headers(response)
    assert sid_headers, response.headers.getlist("Set-Cookie")
    assert all("max-age=0" not in header.lower() for header in sid_headers), sid_headers
    assert all("sid=;" not in header.lower() for header in sid_headers), sid_headers


def test_xhr_request_with_stale_sid_does_not_delete_cookie(app):
    client = app.test_client()
    client.set_cookie("sid", "stale-session")

    response = client.get("/api/app/bootstrap", headers={"X-Requested-With": "XMLHttpRequest"})

    assert response.status_code == 200
    assert response.get_json()["session"]["authenticated"] is False
    sid_headers = _sid_set_cookie_headers(response)
    assert not any("max-age=0" in header.lower() for header in sid_headers), sid_headers


def test_document_request_with_stale_sid_still_clears_cookie(app):
    client = app.test_client()
    client.set_cookie("sid", "stale-session")

    response = client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 200
    sid_headers = _sid_set_cookie_headers(response)
    assert any("max-age=0" in header.lower() for header in sid_headers), sid_headers
