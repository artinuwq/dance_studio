from __future__ import annotations

from datetime import datetime, timedelta
import os
import secrets

from dance_studio.core.time import utcnow

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.core.permissions import ROLES
from dance_studio.db.models import (
    AuthIdentity,
    Base,
    Direction,
    Group,
    GroupAbonement,
    GroupAbonementActionLog,
    PasskeyCredential,
    SessionRecord,
    Staff,
    User,
    UserPhone,
)
from dance_studio.web.app import create_app
from dance_studio.web.services.auth_session import _sid_hash


def _pick_role_with(permission: str) -> str:
    for role, spec in ROLES.items():
        if permission in spec.get("permissions", []):
            return role
    return next(iter(ROLES))


def _pick_role_with_all(*permissions: str) -> str:
    required = set(permissions)
    best_role = None
    best_score = -1
    for role, spec in ROLES.items():
        available = set(spec.get("permissions", []))
        if required.issubset(available):
            return role
        score = len(required.intersection(available))
        if score > best_score:
            best_role = role
            best_score = score
    return best_role or next(iter(ROLES))


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


def _login_by_session(client, db, user_id: int, telegram_id: int | None = None):
    sid = secrets.token_hex(16)
    now = utcnow()
    db.add(
        SessionRecord(
            id=secrets.token_hex(32),
            telegram_id=telegram_id,
            user_id=user_id,
            sid_hash=_sid_hash(sid),
            last_seen=now,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    db.commit()
    client.set_cookie("sid", sid)


def test_admin_can_archive_client_and_archived_clients_leave_listing(app, session_factory):
    db = session_factory()
    admin_user = User(name="Admin", telegram_id=710001)
    active_client = User(name="Visible Client", telegram_id=710002)
    archived_client = User(
        name="Archive Me",
        telegram_id=710003,
        phone="+79990000041",
        primary_phone="+79990000041",
        phone_verified_at=utcnow(),
    )
    db.add_all([admin_user, active_client, archived_client])
    db.commit()

    db.add(Staff(name="Admin Staff", telegram_id=admin_user.telegram_id, user_id=admin_user.id, position=_pick_role_with("verify_certificate"), status="active"))
    db.commit()
    db.add(AuthIdentity(user_id=archived_client.id, provider="telegram", provider_user_id=str(archived_client.telegram_id), is_verified=True))
    db.add(UserPhone(user_id=archived_client.id, phone_e164="+79990000041", verified_at=utcnow(), source="telegram", is_primary=True))
    db.add(PasskeyCredential(user_id=archived_client.id, credential_id="archive-passkey", public_key="pk", sign_count=1))
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, telegram_id=admin_user.telegram_id)

    response = client.post(f"/api/admin/clients/{archived_client.id}/archive")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    db.expire_all()
    archived = db.query(User).filter(User.id == archived_client.id).first()
    assert archived is not None
    assert archived.is_archived is True
    assert archived.status == "inactive"
    assert archived.telegram_id is None
    assert archived.phone is None
    assert archived.primary_phone is None
    assert archived.phone_verified_at is None
    assert db.query(AuthIdentity).filter(AuthIdentity.user_id == archived_client.id).count() == 0
    assert db.query(UserPhone).filter(UserPhone.user_id == archived_client.id).count() == 0
    assert db.query(PasskeyCredential).filter(PasskeyCredential.user_id == archived_client.id).count() == 0
    assert db.query(SessionRecord).filter(SessionRecord.user_id == archived_client.id).count() == 0

    listing = client.get("/users/list/all")
    assert listing.status_code == 200
    listed_ids = {item["id"] for item in listing.get_json()}
    assert active_client.id in listed_ids
    assert archived_client.id not in listed_ids

    db.close()


def test_list_all_users_exposes_vk_identity_fields(app, session_factory):
    db = session_factory()
    admin_user = User(name="Listing Admin", telegram_id=710101)
    client_user = User(name="VK Client", telegram_id=710102, username="vkclient")
    db.add_all([admin_user, client_user])
    db.commit()

    db.add(
        Staff(
            name="Listing Admin Staff",
            telegram_id=admin_user.telegram_id,
            user_id=admin_user.id,
            position=_pick_role_with("verify_certificate"),
            status="active",
        )
    )
    db.add(
        AuthIdentity(
            user_id=client_user.id,
            provider="vk",
            provider_user_id="442972788",
            provider_username="sheba_sport",
            is_verified=True,
        )
    )
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, telegram_id=admin_user.telegram_id)

    response = client.get("/users/list/all")
    assert response.status_code == 200
    payload = response.get_json()
    listed = next(item for item in payload if item["id"] == client_user.id)
    assert "telegram" in listed["auth_providers"]
    assert "vk" in listed["auth_providers"]
    assert listed["vk_user_id"] == "442972788"
    assert listed["vk_username"] == "sheba_sport"

    db.close()


def test_admin_manual_merge_moves_auth_and_phone_entities_and_archives_source(app, session_factory):
    db = session_factory()
    admin_user = User(name="Merge Admin", telegram_id=720001)
    source = User(
        name="Merge Source",
        phone="+79990000051",
        primary_phone="+79990000051",
        phone_verified_at=utcnow(),
        registered_at=datetime(2024, 2, 1, 12, 0, 0),
    )
    target = User(name="Merge Target", telegram_id=720002, registered_at=datetime(2024, 1, 1, 12, 0, 0))
    db.add_all([admin_user, source, target])
    db.commit()

    db.add(Staff(name="Merge Admin Staff", telegram_id=admin_user.telegram_id, user_id=admin_user.id, position=_pick_role_with("verify_certificate"), status="active"))
    db.commit()
    db.add(AuthIdentity(user_id=source.id, provider="vk", provider_user_id="vk-merge-1", is_verified=True))
    db.add(UserPhone(user_id=source.id, phone_e164="+79990000051", verified_at=utcnow(), source="vk", is_primary=True))
    db.add(PasskeyCredential(user_id=source.id, credential_id="merge-passkey", public_key="pk", sign_count=2))
    db.commit()

    sid = secrets.token_hex(16)
    db.add(
        SessionRecord(
            id=secrets.token_hex(32),
            telegram_id=None,
            user_id=source.id,
            sid_hash=_sid_hash(sid),
            last_seen=utcnow(),
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(days=1),
        )
    )
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, telegram_id=admin_user.telegram_id)

    response = client.post(
        "/api/admin/clients/merge",
        json={"source_user_id": source.id, "target_user_id": target.id, "note": "manual test"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["moved"]["sessions"] == 1

    db.expire_all()
    source_after = db.query(User).filter(User.id == source.id).first()
    target_after = db.query(User).filter(User.id == target.id).first()
    assert source_after is not None
    assert target_after is not None
    assert source_after.is_archived is True
    assert source_after.status == "inactive"
    assert source_after.merged_to_user_id == target.id
    assert target_after.phone == "+79990000051"
    assert target_after.primary_phone == "+79990000051"

    moved_identity = db.query(AuthIdentity).filter(AuthIdentity.user_id == target.id, AuthIdentity.provider == "vk").first()
    moved_phone = db.query(UserPhone).filter(UserPhone.user_id == target.id, UserPhone.phone_e164 == "+79990000051").first()
    moved_passkey = db.query(PasskeyCredential).filter(PasskeyCredential.user_id == target.id, PasskeyCredential.credential_id == "merge-passkey").first()
    moved_session = db.query(SessionRecord).filter(SessionRecord.user_id == target.id).count()
    assert moved_identity is not None
    assert moved_phone is not None
    assert moved_phone.is_primary is True
    assert moved_passkey is not None
    assert moved_session >= 1
    assert db.query(AuthIdentity).filter(AuthIdentity.user_id == source.id).count() == 0
    assert db.query(UserPhone).filter(UserPhone.user_id == source.id).count() == 0
    assert db.query(PasskeyCredential).filter(PasskeyCredential.user_id == source.id).count() == 0


def test_admin_manual_merge_keeps_older_profile_and_transfers_telegram_id(app, session_factory):
    db = session_factory()
    admin_user = User(name="Direction Admin", telegram_id=720101)
    older = User(name="Older Account", registered_at=datetime(2024, 1, 1, 9, 0, 0))
    newer = User(name="Newer Account", telegram_id=720102, registered_at=datetime(2024, 2, 1, 9, 0, 0))
    db.add_all([admin_user, older, newer])
    db.commit()

    db.add(
        Staff(
            name="Direction Admin Staff",
            telegram_id=admin_user.telegram_id,
            user_id=admin_user.id,
            position=_pick_role_with("verify_certificate"),
            status="active",
        )
    )
    db.commit()
    db.add(AuthIdentity(user_id=newer.id, provider="telegram", provider_user_id=str(newer.telegram_id), is_verified=True))
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, telegram_id=admin_user.telegram_id)

    response = client.post(
        "/api/admin/clients/merge",
        json={"user_a_id": older.id, "user_b_id": newer.id, "note": "keep oldest"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["kept_user_id"] == older.id
    assert payload["merged_user_id"] == newer.id

    db.expire_all()
    older_after = db.query(User).filter(User.id == older.id).first()
    newer_after = db.query(User).filter(User.id == newer.id).first()
    assert older_after is not None
    assert newer_after is not None
    assert older_after.telegram_id == 720102
    assert newer_after.telegram_id is None
    assert newer_after.merged_to_user_id == older.id

    db.close()


def test_admin_manual_merge_respects_explicit_target_direction(app, session_factory):
    db = session_factory()
    admin_user = User(name="Explicit Merge Admin", telegram_id=720201)
    source = User(name="Explicit Source", registered_at=datetime(2024, 1, 1, 9, 0, 0))
    target = User(name="Explicit Target", telegram_id=720202, registered_at=datetime(2024, 2, 1, 9, 0, 0))
    db.add_all([admin_user, source, target])
    db.commit()

    db.add(
        Staff(
            name="Explicit Merge Staff",
            telegram_id=admin_user.telegram_id,
            user_id=admin_user.id,
            position=_pick_role_with("verify_certificate"),
            status="active",
        )
    )
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, telegram_id=admin_user.telegram_id)

    response = client.post(
        "/api/admin/clients/merge",
        json={"source_user_id": source.id, "target_user_id": target.id, "note": "respect explicit target"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["source_user_id"] == source.id
    assert payload["target_user_id"] == target.id
    assert payload["kept_user_id"] == target.id
    assert payload["merged_user_id"] == source.id

    db.expire_all()
    source_after = db.query(User).filter(User.id == source.id).first()
    target_after = db.query(User).filter(User.id == target.id).first()
    assert source_after is not None
    assert target_after is not None
    assert source_after.is_archived is True
    assert source_after.merged_to_user_id == target.id
    assert target_after.telegram_id == 720202

    db.close()


def test_register_user_can_create_initial_abonements(app, session_factory):
    db = session_factory()
    admin_user = User(name="Create Client Admin", telegram_id=730001)
    db.add(admin_user)
    db.commit()

    admin_staff = Staff(
        name="Create Client Staff",
        telegram_id=admin_user.telegram_id,
        user_id=admin_user.id,
        position=_pick_role_with_all("view_all_users", "verify_certificate"),
        status="active",
    )
    db.add(admin_staff)
    db.commit()

    direction = Direction(title="Contemporary", direction_type="dance", base_price=2400, status="active")
    db.add(direction)
    db.commit()

    group_a = Group(
        direction_id=direction.direction_id,
        teacher_id=admin_staff.id,
        name="Evening A",
        description="Main group",
        age_group="18+",
        max_students=12,
        duration_minutes=60,
        lessons_per_week=2,
    )
    group_b = Group(
        direction_id=direction.direction_id,
        teacher_id=admin_staff.id,
        name="Weekend B",
        description="Weekend group",
        age_group="18+",
        max_students=12,
        duration_minutes=60,
        lessons_per_week=1,
    )
    db.add_all([group_a, group_b])
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, telegram_id=admin_user.telegram_id)

    response = client.post(
        "/users",
        json={
            "name": "Client With Packs",
            "phone": "+79990000071",
            "staff_notes": "Prefers evening groups",
            "initial_abonements": [
                {
                    "group_id": group_a.id,
                    "abonement_type": "multi",
                    "status": "active",
                    "weeks": 4,
                    "price_total_rub": 2400,
                    "note": "First pack",
                },
                {
                    "group_id": group_b.id,
                    "abonement_type": "single",
                    "status": "pending_payment",
                    "lessons": 1,
                    "price_total_rub": 700,
                    "note": "Second pack",
                },
            ],
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["name"] == "Client With Packs"
    assert len(payload["initial_abonements"]) == 2
    assert payload["initial_abonements"][0]["bundle_size"] == 1
    assert payload["initial_abonements"][1]["issued_abonements"][0]["status"] == "pending_payment"

    db.expire_all()
    created_user = db.query(User).filter(User.name == "Client With Packs").first()
    assert created_user is not None
    abonements = (
        db.query(GroupAbonement)
        .filter(GroupAbonement.user_id == created_user.id)
        .order_by(GroupAbonement.id.asc())
        .all()
    )
    assert len(abonements) == 2
    assert abonements[0].group_id == group_a.id
    assert abonements[0].status == "active"
    assert abonements[1].group_id == group_b.id
    assert abonements[1].status == "pending_payment"
    assert db.query(GroupAbonementActionLog).join(GroupAbonement, GroupAbonement.id == GroupAbonementActionLog.abonement_id).filter(
        GroupAbonement.user_id == created_user.id
    ).count() == 2

    db.close()


def test_register_user_rejects_more_than_three_initial_abonements(app, session_factory):
    db = session_factory()
    admin_user = User(name="Create Client Limit Admin", telegram_id=730101)
    db.add(admin_user)
    db.commit()

    db.add(
        Staff(
            name="Create Client Limit Staff",
            telegram_id=admin_user.telegram_id,
            user_id=admin_user.id,
            position=_pick_role_with_all("view_all_users", "verify_certificate"),
            status="active",
        )
    )
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, admin_user.id, telegram_id=admin_user.telegram_id)

    response = client.post(
        "/users",
        json={
            "name": "Too Many Packs",
            "initial_abonements": [{}, {}, {}, {}],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Можно добавить максимум 3 абонемента при создании клиента"
    assert db.query(User).filter(User.name == "Too Many Packs").count() == 0

    db.close()

