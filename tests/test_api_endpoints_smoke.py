from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.core.config import OWNER_IDS, TECH_ADMIN_ID
from dance_studio.core.permissions import ROLES
from dance_studio.db.models import Base, SessionRecord, Staff, User
from dance_studio.web.app import create_app
from dance_studio.web.services.auth_session import _sid_hash


def _iter_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"})
        for method in methods:
            yield rule, method


def _fill_rule_path(rule) -> str:
    path = rule.rule
    for arg in rule.arguments:
        converter = rule._converters[arg]
        name = converter.__class__.__name__.lower()
        if "int" in name:
            value = "1"
        elif "path" in name:
            value = "key"
        else:
            value = "token"
        path = re.sub(rf"<[^:>]+:{arg}>", value, path)
        path = path.replace(f"<{arg}>", value)
    return path


def _pick_admin_position() -> str:
    for role, spec in ROLES.items():
        if "full_system_access" in spec.get("permissions", []):
            return role
    for role, spec in ROLES.items():
        if "manage_staff" in spec.get("permissions", []):
            return role
    return next(iter(ROLES))


def _pick_non_admin_position() -> str:
    for role, spec in ROLES.items():
        perms = set(spec.get("permissions", []))
        if "manage_staff" not in perms and "manage_schedule" not in perms and "system_settings" not in perms:
            return role
    return next(iter(ROLES))


def _pick_free_telegram_id(start: int) -> int:
    owner_ids = set(OWNER_IDS or [])
    current = start
    while current == TECH_ADMIN_ID or current in owner_ids:
        current += 1
    return current


def _seed_staff(db, telegram_id: int, position: str) -> Staff:
    staff = Staff(
        name=f"Staff {telegram_id}",
        telegram_id=telegram_id,
        position=position,
        status="active",
    )
    db.add(staff)
    db.flush()
    return staff


def _seed_user(db, telegram_id: int) -> User:
    user = User(
        name=f"User {telegram_id}",
        telegram_id=telegram_id,
    )
    db.add(user)
    db.flush()
    return user


def _create_session(db, telegram_id: int) -> str:
    sid = secrets.token_hex(16)
    now = datetime.utcnow()
    record = SessionRecord(
        id=secrets.token_hex(32),
        telegram_id=telegram_id,
        user_agent_hash=None,
        sid_hash=_sid_hash(sid),
        ip_prefix=None,
        need_reauth=False,
        reauth_reason=None,
        last_seen=now,
        created_at=now,
        expires_at=now + timedelta(days=1),
    )
    db.add(record)
    db.commit()
    return sid


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def test_all_endpoints_smoke(app):
    client = app.test_client()
    for rule, method in _iter_rules(app):
        url = _fill_rule_path(rule)
        response = client.open(url, method=method)
        assert response.status_code < 500, f"{method} {url} returned {response.status_code}"


def test_admin_endpoints_require_admin(app, session_factory):
    db = session_factory()
    try:
        admin_position = _pick_admin_position()
        non_admin_position = _pick_non_admin_position()
        admin_id = _pick_free_telegram_id(1000)
        non_admin_id = _pick_free_telegram_id(admin_id + 1)

        _seed_staff(db, admin_id, admin_position)
        _seed_staff(db, non_admin_id, non_admin_position)
        _seed_user(db, admin_id)
        _seed_user(db, non_admin_id)
        db.commit()

        admin_sid = _create_session(db, admin_id)
        non_admin_sid = _create_session(db, non_admin_id)
    finally:
        db.close()

    admin_client = app.test_client()
    admin_client.set_cookie("localhost", "sid", admin_sid)

    non_admin_client = app.test_client()
    non_admin_client.set_cookie("localhost", "sid", non_admin_sid)

    for rule, method in _iter_rules(app):
        if not rule.rule.startswith("/api/admin/"):
            continue
        url = _fill_rule_path(rule)

        non_admin_response = non_admin_client.open(url, method=method)
        assert non_admin_response.status_code == 403, (
            f"{method} {url} expected 403 for non-admin, got {non_admin_response.status_code}"
        )

        admin_response = admin_client.open(url, method=method)
        assert admin_response.status_code < 500, (
            f"{method} {url} returned {admin_response.status_code} for admin"
        )
        assert admin_response.status_code not in {401, 403}, (
            f"{method} {url} expected admin access, got {admin_response.status_code}"
        )
