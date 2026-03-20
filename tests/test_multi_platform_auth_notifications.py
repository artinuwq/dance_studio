from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.auth.services.account_merge import AccountMergeService
from dance_studio.auth.services.common import ensure_user_phone, get_or_create_identity
from dance_studio.db.models import AuthIdentity, Base, PasskeyChallenge, PasskeyCredential, SessionRecord, Staff, User, UserMergeEvent, UserPhone
from dance_studio.web.app import create_app
from dance_studio.web.services.auth_session import _sid_hash


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
    monkeypatch.setattr("dance_studio.auth.providers.telegram.validate_init_data", lambda _: type("V", (), {"user_id": 777, "replay_key": "rk"})())
    monkeypatch.setattr("dance_studio.web.routes.auth.store_used_init_data", lambda *args, **kwargs: True)
    return create_app()


def _login_by_session(client, db, user_id: int, telegram_id: int | None = None):
    sid = secrets.token_hex(16)
    now = datetime.utcnow()
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


def _vk_payload(**overrides):
    payload = {"vk_ts": "1710000000", "vk_user_id": "12345", "name": "VK User"}
    payload.update(overrides)
    base = "&".join(f"{key}={payload[key]}" for key in sorted(payload)) + "test-secret"
    payload["sign"] = hashlib.md5(base.encode("utf-8")).hexdigest()
    return payload


def _b64url_json(payload: dict) -> str:
    import base64

    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")


def _passkey_signature(public_key: str, authenticator_data: str, client_data_json: str) -> str:
    import base64

    digest = hmac.new(
        public_key.encode("utf-8"),
        f"{authenticator_data}.{client_data_json}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_auth_vk_and_phone_flow(app, session_factory, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    client = app.test_client()

    vk_resp = client.post("/auth/vk", json=_vk_payload())
    assert vk_resp.status_code == 200

    request_code = client.post("/auth/phone/request-code", json={"phone": "8 (999) 000-00-00"})
    assert request_code.status_code == 200
    code = request_code.get_json()["debug_code"]

    verify = client.post("/auth/phone/verify-code", json={"phone": "+79990000000", "code": code})
    assert verify.status_code == 200

    db = session_factory()
    phone = db.query(UserPhone).filter(UserPhone.phone_e164 == "+79990000000").first()
    assert phone is not None
    assert phone.verified_at is not None
    assert phone.is_primary is True


def test_vk_signature_is_required(app, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    client = app.test_client()
    resp = client.post("/auth/vk", json={"vk_user_id": "12345", "name": "VK User", "sign": "bad"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_vk_signature"


def test_verified_phone_links_new_vk_identity_to_existing_user(app, session_factory, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    db = session_factory()
    user = User(name="Existing")
    db.add(user)
    db.commit()
    db.add(UserPhone(user_id=user.id, phone_e164="+79991112233", verified_at=datetime.utcnow(), source="sms", is_primary=True))
    db.commit()

    client = app.test_client()
    resp = client.post(
        "/auth/vk",
        json=_vk_payload(vk_user_id="55", phone="+7 (999) 111-22-33", phone_verified=True),
    )
    assert resp.status_code == 200
    assert resp.get_json()["user_id"] == user.id


def test_passkey_register_duplicate_delete_and_login(app, session_factory):
    db = session_factory()
    user = User(name="Passkey User", telegram_id=777001)
    db.add(user)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    begin = client.post("/auth/passkey/register/begin", json={})
    assert begin.status_code == 200
    begin_payload = begin.get_json()
    assert begin_payload["status"] == "ok"
    assert begin_payload["fallback_auth_methods"] == ["telegram", "vk", "phone"]
    assert begin_payload["publicKey"]["rp"]["id"] == "localhost"

    client_data_json = _b64url_json(
        {"type": "webauthn.create", "challenge": begin_payload["challenge"], "origin": begin_payload["origin"]}
    )
    attestation_object = _b64url_json(
        {
            "rpId": begin_payload["rp_id"],
            "credentialId": "cred-1",
            "publicKey": "pk-1",
            "signCount": 1,
            "transports": ["internal"],
            "deviceName": "Browser Passkey",
        }
    )

    complete = client.post(
        "/auth/passkey/register/complete",
        json={"credential": {"id": "cred-1", "type": "public-key", "response": {"clientDataJSON": client_data_json, "attestationObject": attestation_object}}},
    )
    assert complete.status_code == 200

    duplicate_begin = client.post("/auth/passkey/register/begin", json={})
    duplicate_payload = duplicate_begin.get_json()
    duplicate = client.post(
        "/auth/passkey/register/complete",
        json={
            "credential": {
                "id": "cred-1",
                "type": "public-key",
                "response": {
                    "clientDataJSON": _b64url_json({"type": "webauthn.create", "challenge": duplicate_payload["challenge"], "origin": duplicate_payload["origin"]}),
                    "attestationObject": _b64url_json({"rpId": duplicate_payload["rp_id"], "credentialId": "cred-1", "publicKey": "pk-1", "signCount": 2}),
                },
            }
        },
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["error"] == "duplicate_passkey"

    listed = client.get("/auth/passkeys")
    assert listed.status_code == 200
    assert listed.get_json()["items"][0]["credential_id"] == "cred-1"

    login_begin = client.post("/auth/passkey/login/begin", json={})
    assert login_begin.status_code == 200
    login_begin_payload = login_begin.get_json()
    login_client_data = _b64url_json(
        {"type": "webauthn.get", "challenge": login_begin_payload["challenge"], "origin": login_begin_payload["origin"]}
    )
    authenticator_data = _b64url_json({"rpId": login_begin_payload["rp_id"], "signCount": 2, "userPresent": True})
    signature = _passkey_signature("pk-1", authenticator_data, login_client_data)
    login = client.post(
        "/auth/passkey/login/complete",
        json={"credential": {"id": "cred-1", "type": "public-key", "response": {"clientDataJSON": login_client_data, "authenticatorData": authenticator_data, "signature": signature}}},
    )
    assert login.status_code == 200
    assert login.get_json()["user_id"] == user.id

    bad_login_begin = client.post("/auth/passkey/login/begin", json={})
    bad_begin_payload = bad_login_begin.get_json()
    bad_client_data = _b64url_json(
        {"type": "webauthn.get", "challenge": bad_begin_payload["challenge"], "origin": bad_begin_payload["origin"]}
    )
    bad_authenticator_data = _b64url_json({"rpId": bad_begin_payload["rp_id"], "signCount": 1, "userPresent": True})
    bad_signature = _passkey_signature("pk-1", bad_authenticator_data, bad_client_data)
    bad_counter = client.post(
        "/auth/passkey/login/complete",
        json={"credential": {"id": "cred-1", "type": "public-key", "response": {"clientDataJSON": bad_client_data, "authenticatorData": bad_authenticator_data, "signature": bad_signature}}},
    )
    assert bad_counter.status_code == 400
    assert bad_counter.get_json()["fallback_auth_methods"] == ["telegram", "vk", "phone"]

    delete_resp = client.post("/auth/passkey/delete", json={"credential_id": "cred-1"})
    assert delete_resp.status_code == 200

    db.expire_all()
    cred = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == "cred-1").first()
    assert cred is None
    assert db.query(PasskeyChallenge).count() >= 1


def test_multiple_phones_switches_primary(session_factory):
    db = session_factory()
    user = User(name="Phones")
    db.add(user)
    db.commit()

    p1 = ensure_user_phone(db, user_id=user.id, phone_e164="+79990000001", source="sms", verified_at=datetime.utcnow(), is_primary=True)
    p2 = ensure_user_phone(db, user_id=user.id, phone_e164="+79990000002", source="telegram", verified_at=datetime.utcnow(), is_primary=True)
    db.commit()

    db.refresh(p1)
    db.refresh(p2)
    assert p1.is_primary is False
    assert p2.is_primary is True


def test_merge_conflict_and_manual_review_are_logged(session_factory):
    db = session_factory()
    db.execute(text("DROP INDEX IF EXISTS ix_user_phones_verified_phone_unique"))
    source = User(name="Source")
    target1 = User(name="T1")
    target2 = User(name="T2")
    db.add_all([source, target1, target2])
    db.commit()
    now = datetime.utcnow()
    db.add_all(
        [
            UserPhone(user_id=target1.id, phone_e164="+79990000003", verified_at=now, source="sms", is_primary=True),
            UserPhone(user_id=target2.id, phone_e164="+79990000003", verified_at=now, source="sms", is_primary=True),
        ]
    )
    db.commit()

    result = AccountMergeService().try_merge_by_phone(db, user_id=source.id, phone="+79990000003", source="test")
    db.commit()

    assert result["status"] == "conflict"
    event = db.query(UserMergeEvent).order_by(UserMergeEvent.id.desc()).first()
    assert event.merge_reason == "phone_conflict"
    db.query(UserPhone).filter(UserPhone.phone_e164 == "+79990000003").delete(synchronize_session=False)
    db.commit()
    db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_phones_verified_phone_unique ON user_phones(phone_e164) WHERE verified_at IS NOT NULL"))
    db.commit()


def test_manual_merge_flag_blocks_auto_merge(session_factory):
    db = session_factory()
    source = User(name="Source")
    target = User(name="Target", requires_manual_merge=True)
    db.add_all([source, target])
    db.commit()
    db.add(UserPhone(user_id=target.id, phone_e164="+79990000004", verified_at=datetime.utcnow(), source="sms", is_primary=True))
    db.commit()

    result = AccountMergeService().try_merge_by_phone(db, user_id=source.id, phone="+79990000004", source="test")
    db.commit()

    assert result["status"] == "manual_review_required"


def test_parallel_login_with_same_verified_phone_does_not_duplicate_identity(session_factory, tmp_path):
    race_db_url = f"sqlite:///{tmp_path / 'auth_race.db'}"
    race_engine = create_engine(race_db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(race_engine)
    race_session_factory = sessionmaker(bind=race_engine, autoflush=False, autocommit=False)

    def worker(provider_user_id: str):
        db = race_session_factory()
        user = get_or_create_identity(
            db,
            provider="vk",
            provider_user_id=provider_user_id,
            username=None,
            payload_json="{}",
            fallback_name="Concurrent",
            verified_phone="+79990000005",
        )
        db.commit()
        return user.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, ["parallel-1", "parallel-2"]))

    assert len(set(results)) == 1

    db = race_session_factory()
    users = db.query(User).filter(User.name == "Concurrent", User.is_archived.is_(False)).all()
    identities = db.query(AuthIdentity).filter(AuthIdentity.provider == "vk", AuthIdentity.provider_user_id.in_(["parallel-1", "parallel-2"])).all()
    verified_phones = db.query(UserPhone).filter(UserPhone.phone_e164 == "+79990000005", UserPhone.verified_at.isnot(None)).all()
    assert len(users) == 1
    assert len(identities) == 2
    assert len({row.user_id for row in identities}) == 1
    assert len(verified_phones) == 1


def test_race_condition_on_identity_link_keeps_single_target_user(session_factory):
    db = session_factory()
    user = User(name="Existing Link")
    db.add(user)
    db.commit()
    db.add(UserPhone(user_id=user.id, phone_e164="+79990000006", verified_at=datetime.utcnow(), source="sms", is_primary=True))
    db.commit()

    def worker(provider_user_id: str):
        session = session_factory()
        linked_user = get_or_create_identity(
            session,
            provider="telegram",
            provider_user_id=provider_user_id,
            username=None,
            payload_json="{}",
            fallback_name="Will Not Be Created",
            verified_phone="+79990000006",
        )
        session.commit()
        return linked_user.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, ["tg-race-1", "tg-race-2"]))

    assert results == [user.id, user.id]



def test_app_bootstrap_returns_user_centric_contract(app, session_factory):
    db = session_factory()
    user = User(name="Bootstrap User", telegram_id=321321)
    db.add(user)
    db.commit()
    db.add(UserPhone(user_id=user.id, phone_e164="+79990000007", verified_at=datetime.utcnow(), source="sms", is_primary=True))
    db.add(AuthIdentity(user_id=user.id, provider="telegram", provider_user_id="321321", is_verified=True))
    db.add(PasskeyCredential(user_id=user.id, credential_id="bootstrap-passkey", public_key="pk", sign_count=1))
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    response = client.get('/api/app/bootstrap')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['session']['authenticated'] is True
    assert payload['user']['id'] == user.id
    assert payload['user']['phone_verified'] is True
    assert payload['user']['identities']['telegram']['linked'] is True
    assert payload['user']['identities']['passkey']['count'] == 1
    assert payload['user']['identities']['passkey']['items'][0]['credential_id'] == 'bootstrap-passkey'
    assert payload['user']['deprecated']['legacy_user_fields']['telegram_id'] == 321321
    assert payload['feature_flags']['passkey_scaffold'] is False
    assert payload['feature_flags']['passkey_webauthn'] is True

def test_notifications_preferences_and_web_push(app, session_factory):
    db = session_factory()
    user = User(name="Tester", telegram_id=9001)
    db.add(user)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    p = client.post("/api/notifications/preferences", json={"event_type": "lesson_reminder", "channel_type": "telegram", "priority": 1, "is_enabled": True})
    assert p.status_code == 200

    s = client.post("/api/notifications/web-push/subscribe", json={"endpoint": "https://push.example/1", "keys": {"p256dh": "k", "auth": "a"}})
    assert s.status_code == 200

    send = client.post("/api/notifications/test-send", json={"event_type": "lesson_reminder", "title": "A", "body": "B"})
    assert send.status_code == 200


def test_account_merge_preview_and_confirm(app, session_factory):
    db = session_factory()
    u1 = User(name="A", telegram_id=10001)
    u2 = User(name="B", telegram_id=10002)
    db.add_all([u1, u2])
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, u1.id, telegram_id=u1.telegram_id)
    preview = client.post("/api/account/merge/preview", json={"user_a_id": u1.id, "user_b_id": u2.id})
    assert preview.status_code == 200

    confirm = client.post("/api/account/merge/confirm", json={"user_a_id": u1.id, "user_b_id": u2.id, "reason": "test"})
    assert confirm.status_code == 200


def test_vk_link_flow_returns_link_contract_and_bootstrap_updates(app, session_factory, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    db = session_factory()
    user = User(name="Link Me", telegram_id=40001)
    db.add(user)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    response = client.post("/auth/vk", json=_vk_payload(vk_user_id="link-55", name="VK Link User"))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["linked"] is True
    assert payload["provider"] == "vk"
    assert payload["user_id"] == user.id
    assert payload["identities"]["vk"]["linked"] is True

    bootstrap = client.get("/api/app/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.get_json()["user"]["identities"]["vk"]["linked"] is True


def test_manual_merge_queue_admin_review_flow(app, session_factory):
    db = session_factory()
    staff = Staff(name="Manager", telegram_id=50001, position="владелец", status="active")
    source = User(name="Merge Source")
    target = User(name="Merge Target", requires_manual_merge=True)
    db.add_all([staff, source, target])
    db.commit()
    db.add(UserPhone(user_id=target.id, phone_e164="+79997770000", verified_at=datetime.utcnow(), source="sms", is_primary=True))
    db.commit()

    AccountMergeService().try_merge_by_phone(db, user_id=source.id, phone="+79997770000", source="review_test")
    db.commit()
    event = db.query(UserMergeEvent).order_by(UserMergeEvent.id.desc()).first()
    assert event.case_status == "pending_review"

    client = app.test_client()
    _login_by_session(client, db, source.id, telegram_id=staff.telegram_id)

    listing = client.get("/api/admin/manual-merge-cases")
    assert listing.status_code == 200
    listed_ids = {item["id"] for item in listing.get_json()["items"]}
    assert event.id in listed_ids

    details = client.get(f"/api/admin/manual-merge-cases/{event.id}")
    assert details.status_code == 200
    assert details.get_json()["case_status"] == "pending_review"

    review = client.post(f"/api/admin/manual-merge-cases/{event.id}/review", json={"decision": "ignore", "reason": "False positive"})
    assert review.status_code == 200
    reviewed_case = review.get_json()["case"]
    assert reviewed_case["review_result"] == "ignored"
    assert reviewed_case["case_status"] == "ignored"
