from __future__ import annotations

from datetime import datetime, timedelta
import base64
import hashlib
import hmac
import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from time import sleep

from dance_studio.core.time import utcnow
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import dance_studio.db as db_module
import dance_studio.web.middleware.auth as auth_middleware
from dance_studio.auth.services.account_merge import AccountMergeService
from dance_studio.auth.services.common import ensure_user_phone, get_or_create_identity
from dance_studio.core.permissions import ROLES
from dance_studio.db.models import AuthIdentity, Base, NotificationChannel, PasskeyChallenge, PasskeyCredential, SessionRecord, Staff, User, UserMergeEvent, UserPhone
from dance_studio.web.app import create_app
from dance_studio.web.services.auth_session import _sid_hash
from dance_studio.web.services import bookings as booking_services


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


def _vk_payload(**overrides):
    payload = {"vk_ts": "1710000000", "vk_user_id": "12345", "vk_access_token_settings": "", "name": "VK User"}
    payload.update(overrides)
    signed_part = {key: value for key, value in payload.items() if str(key).startswith("vk_")}
    params = urlencode(sorted(signed_part.items()), doseq=True)
    digest = hmac.new(b"test-secret", params.encode("utf-8"), hashlib.sha256).digest()
    payload["sign"] = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
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


def test_telegram_auth_duplicate_bootstrap_reuses_recent_session(app, session_factory, monkeypatch):
    replay_calls = {"count": 0}

    def _store_used_init_data(*args, **kwargs):
        replay_calls["count"] += 1
        return replay_calls["count"] == 1

    monkeypatch.setattr("dance_studio.web.routes.auth.store_used_init_data", _store_used_init_data)

    headers = {"User-Agent": "telegram-bootstrap-test"}
    first_client = app.test_client()
    first = first_client.post("/auth/telegram", json={"init_data": "dummy"}, headers=headers)
    assert first.status_code == 200
    first_payload = first.get_json()

    second_client = app.test_client()
    second = second_client.post("/auth/telegram", json={"init_data": "dummy"}, headers=headers)
    assert second.status_code == 200

    payload = second.get_json()
    assert payload["user_id"] == first_payload["user_id"]

    db = session_factory()
    sessions = db.query(SessionRecord).filter(SessionRecord.user_id == payload["user_id"]).all()
    assert len(sessions) >= 2


def test_telegram_auth_uses_telegram_profile_name_for_new_user(app, session_factory, monkeypatch):
    monkeypatch.setattr(
        "dance_studio.auth.providers.telegram.validate_init_data",
        lambda _: type(
            "V",
            (),
            {
                "user_id": 778,
                "replay_key": "rk-name",
                "first_name": "Анна",
                "last_name": "Иванова",
                "username": "anna_ivanova",
            },
        )(),
    )
    client = app.test_client()

    response = client.post("/auth/telegram", json={"init_data": "dummy"})
    assert response.status_code == 200

    db = session_factory()
    user = db.query(User).filter(User.telegram_id == 778).first()
    assert user is not None
    assert user.name == "Анна Иванова"
    assert user.username == "anna_ivanova"


def test_telegram_auth_upgrades_generated_name_from_telegram_profile(app, session_factory, monkeypatch):
    db = session_factory()
    user = User(name="Telegram 779", telegram_id=779)
    db.add(user)
    db.commit()

    monkeypatch.setattr(
        "dance_studio.auth.providers.telegram.validate_init_data",
        lambda _: type(
            "V",
            (),
            {
                "user_id": 779,
                "replay_key": "rk-upgrade",
                "first_name": "Мария",
                "last_name": "Петрова",
                "username": "maria_pet",
            },
        )(),
    )
    client = app.test_client()

    response = client.post("/auth/telegram", json={"init_data": "dummy"})
    assert response.status_code == 200

    db.expire_all()
    refreshed = db.query(User).filter(User.id == user.id).first()
    assert refreshed is not None
    assert refreshed.name == "Мария Петрова"
    assert refreshed.username == "maria_pet"


def test_phone_request_code_rejects_unknown_phone_for_anonymous_user(app):
    client = app.test_client()
    response = client.post("/auth/phone/request-code", json={"phone": "+79995556677"})
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["error"] == "phone_not_linked"
    assert payload["action"] == "use_mini_app"


def test_vk_signature_is_required(app, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    client = app.test_client()
    resp = client.post("/auth/vk", json={"vk_user_id": "12345", "name": "VK User", "sign": "bad"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_vk_signature"


def test_vk_signature_ignores_non_vk_fields(app, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    client = app.test_client()
    resp = client.post("/auth/vk", json=_vk_payload(screen_name="vk_user_profile"))
    assert resp.status_code == 200


def test_vk_signature_ignores_non_launch_vk_fields(app, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    client = app.test_client()
    payload = _vk_payload()
    payload["vk_username"] = "VK Display Name"
    resp = client.post("/auth/vk", json=payload)
    assert resp.status_code == 200


def test_verified_phone_links_new_vk_identity_to_existing_user(app, session_factory, monkeypatch):
    monkeypatch.setattr("dance_studio.auth.providers.vk.APP_SECRET_KEY", "test-secret")
    db = session_factory()
    user = User(name="Existing")
    db.add(user)
    db.commit()
    db.add(UserPhone(user_id=user.id, phone_e164="+79991112233", verified_at=utcnow(), source="sms", is_primary=True))
    db.commit()

    client = app.test_client()
    resp = client.post(
        "/auth/vk",
        json=_vk_payload(vk_user_id="55", phone="+7 (999) 111-22-33", phone_verified=True),
    )
    assert resp.status_code == 200
    assert resp.get_json()["user_id"] == user.id


def test_auth_vk_phone_merges_into_existing_verified_phone_without_duplicate_rows(app, session_factory):
    db = session_factory()
    source = User(name="Source", telegram_id=70001)
    target = User(name="Target")
    db.add_all([source, target])
    db.commit()
    db.add(UserPhone(user_id=target.id, phone_e164="+79995558394", verified_at=utcnow(), source="sms", is_primary=True))
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, source.id, telegram_id=source.telegram_id)

    response = client.post("/auth/vk/phone", json={"phone": "+7 (999) 555-83-94"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["merge_status"] == "merged"
    assert payload["merge_notice"] == "Аккаунты объединены. Проверьте, что все данные на месте."
    assert payload["phone"] == "+79995558394"

    db.expire_all()
    merged_source = db.query(User).filter(User.id == source.id).first()
    merged_target = db.query(User).filter(User.id == target.id).first()
    assert merged_source is not None
    assert merged_target is not None
    assert merged_source.is_archived is False
    assert merged_target.is_archived is True
    verified_rows = db.query(UserPhone).filter(UserPhone.phone_e164 == "+79995558394", UserPhone.verified_at.isnot(None)).all()
    assert len(verified_rows) == 1
    assert verified_rows[0].user_id == source.id


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

    # Synced passkeys may report a zero counter; allow 0 -> 0 progression.
    db.expire_all()
    stored_credential = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == "cred-1").first()
    assert stored_credential is not None
    stored_credential.sign_count = 0
    db.commit()

    zero_login_begin = client.post("/auth/passkey/login/begin", json={})
    zero_begin_payload = zero_login_begin.get_json()
    zero_client_data = _b64url_json(
        {"type": "webauthn.get", "challenge": zero_begin_payload["challenge"], "origin": zero_begin_payload["origin"]}
    )
    zero_authenticator_data = _b64url_json({"rpId": zero_begin_payload["rp_id"], "signCount": 0, "userPresent": True})
    zero_signature = _passkey_signature("pk-1", zero_authenticator_data, zero_client_data)
    zero_counter_login = client.post(
        "/auth/passkey/login/complete",
        json={"credential": {"id": "cred-1", "type": "public-key", "response": {"clientDataJSON": zero_client_data, "authenticatorData": zero_authenticator_data, "signature": zero_signature}}},
    )
    assert zero_counter_login.status_code == 200
    assert zero_counter_login.get_json()["user_id"] == user.id

    db.expire_all()
    post_zero_credential = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == "cred-1").first()
    assert post_zero_credential is not None
    post_zero_credential.sign_count = 2
    db.commit()

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

    p1 = ensure_user_phone(db, user_id=user.id, phone_e164="+79990000001", source="sms", verified_at=utcnow(), is_primary=True)
    p2 = ensure_user_phone(db, user_id=user.id, phone_e164="+79990000002", source="telegram", verified_at=utcnow(), is_primary=True)
    db.commit()

    db.refresh(p1)
    db.refresh(p2)
    assert p1.is_primary is False
    assert p2.is_primary is True


def test_try_merge_by_phone_matches_legacy_client_phone(session_factory):
    db = session_factory()
    source = User(name="Telegram User", telegram_id=99001)
    target = User(name="Legacy Client", phone="8 (999) 777-00-00")
    db.add_all([source, target])
    db.commit()

    result = AccountMergeService().try_merge_by_phone(db, user_id=source.id, phone="+79997770000", source="telegram_contact")
    db.commit()

    assert result["status"] == "merged"

    db.expire_all()
    primary = db.query(User).filter(User.id == result["primary_user_id"]).first()
    secondary = db.query(User).filter(User.id == result["secondary_user_id"]).first()
    assert primary is not None
    assert secondary is not None
    assert secondary.is_archived is True

    merged_phone = db.query(UserPhone).filter(UserPhone.user_id == primary.id, UserPhone.is_primary.is_(True)).first()
    assert merged_phone is not None
    assert merged_phone.phone_e164 == "+79997770000"
    assert merged_phone.verified_at is not None


def test_merge_conflict_and_manual_review_are_logged(session_factory):
    db = session_factory()
    db.execute(text("DROP INDEX IF EXISTS ix_user_phones_verified_phone_unique"))
    source = User(name="Source")
    target1 = User(name="T1")
    target2 = User(name="T2")
    db.add_all([source, target1, target2])
    db.commit()
    now = utcnow()
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
    db.add(UserPhone(user_id=target.id, phone_e164="+79990000004", verified_at=utcnow(), source="sms", is_primary=True))
    db.commit()

    result = AccountMergeService().try_merge_by_phone(db, user_id=source.id, phone="+79990000004", source="test")
    db.commit()

    assert result["status"] == "manual_review_required"


def test_parallel_login_with_same_verified_phone_does_not_duplicate_identity():
    race_db_url = f"sqlite:///file:auth_race_{secrets.token_hex(8)}?mode=memory&cache=shared&uri=true"
    race_engine = create_engine(
        race_db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(race_engine)
    race_session_factory = sessionmaker(bind=race_engine, autoflush=False, autocommit=False)

    def worker(provider_user_id: str):
        for attempt in range(5):
            db = race_session_factory()
            try:
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
            except OperationalError as exc:
                db.rollback()
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                sleep(0.05 * (attempt + 1))
            finally:
                db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, ["parallel-1", "parallel-2"]))

    assert len(set(results)) == 1

    db = race_session_factory()
    try:
        users = db.query(User).filter(User.name == "Concurrent", User.is_archived.is_(False)).all()
        identities = db.query(AuthIdentity).filter(AuthIdentity.provider == "vk", AuthIdentity.provider_user_id.in_(["parallel-1", "parallel-2"])).all()
        verified_phones = db.query(UserPhone).filter(UserPhone.phone_e164 == "+79990000005", UserPhone.verified_at.isnot(None)).all()
        assert len(users) == 1
        assert len(identities) == 2
        assert len({row.user_id for row in identities}) == 1
        assert len(verified_phones) == 1
    finally:
        db.close()


def test_race_condition_on_identity_link_keeps_single_target_user(session_factory):
    db = session_factory()
    user = User(name="Existing Link")
    db.add(user)
    db.commit()
    db.add(UserPhone(user_id=user.id, phone_e164="+79990000006", verified_at=utcnow(), source="sms", is_primary=True))
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
    db.add(UserPhone(user_id=user.id, phone_e164="+79990000007", verified_at=utcnow(), source="sms", is_primary=True))
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


def test_app_bootstrap_includes_staff_snapshot_for_authenticated_staff(app, session_factory):
    db = session_factory()
    user = User(name="Bootstrap Staff User", telegram_id=321322)
    db.add(user)
    db.commit()
    db.add(Staff(user_id=user.id, telegram_id=user.telegram_id, name="Bootstrap Staff", position="Администратор", status="active"))
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    response = client.get('/api/app/bootstrap')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['staff']['is_staff'] is True
    assert payload['staff']['staff']['position'] == 'Администратор'
    assert payload['staff']['staff']['name'] == 'Bootstrap Staff'

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


def test_vk_primary_channel_requires_permission_until_verified(app, session_factory):
    db = session_factory()
    user = User(name="VK Channel User", telegram_id=9011)
    db.add(user)
    db.commit()
    vk_channel = NotificationChannel(
        user_id=user.id,
        channel_type="vk",
        target_ref="778899",
        is_enabled=True,
        is_primary=False,
        is_verified=False,
    )
    db.add(vk_channel)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    channels = client.get("/api/notifications/channels")
    assert channels.status_code == 200
    items = channels.get_json()["items"]
    vk_item = next(item for item in items if item["id"] == vk_channel.id)
    assert vk_item["is_verified"] is False

    blocked = client.post("/api/notifications/channels/select-primary", json={"channel_id": vk_channel.id})
    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "vk_permission_required"

    mark_verified = client.post("/api/notifications/channels/vk/mark-verified", json={"channel_id": vk_channel.id})
    assert mark_verified.status_code == 400
    assert mark_verified.get_json()["error"] == "vk_permission_key_required"

    permission = client.post("/api/notifications/channels/vk/request-permission", json={"channel_id": vk_channel.id})
    assert permission.status_code == 200
    permission_payload = permission.get_json()
    assert permission_payload["ok"] is True
    assert permission_payload["channel"]["id"] == vk_channel.id
    assert permission_payload["permission_key"]

    bad_mark = client.post(
        "/api/notifications/channels/vk/mark-verified",
        json={"channel_id": vk_channel.id, "permission_key": "invalid"},
    )
    assert bad_mark.status_code == 409
    assert bad_mark.get_json()["error"] == "vk_permission_key_invalid"

    mark_verified = client.post(
        "/api/notifications/channels/vk/mark-verified",
        json={"channel_id": vk_channel.id, "permission_key": permission_payload["permission_key"]},
    )
    assert mark_verified.status_code == 200
    assert mark_verified.get_json()["ok"] is True
    assert mark_verified.get_json()["channel"]["is_verified"] is True

    selected = client.post("/api/notifications/channels/select-primary", json={"channel_id": vk_channel.id})
    assert selected.status_code == 200
    assert selected.get_json()["ok"] is True


def test_vk_permission_request_exposes_configured_community_id(app, session_factory, monkeypatch):
    monkeypatch.setattr("dance_studio.web.routes.platform_api.VK_COMMUNITY_ID", "123456789")
    db = session_factory()
    user = User(name="VK Community Config User", telegram_id=9012)
    db.add(user)
    db.commit()
    vk_channel = NotificationChannel(
        user_id=user.id,
        channel_type="vk",
        target_ref="998877",
        is_enabled=True,
        is_primary=False,
        is_verified=False,
    )
    db.add(vk_channel)
    db.commit()

    client = app.test_client()
    _login_by_session(client, db, user.id, telegram_id=user.telegram_id)

    permission = client.post("/api/notifications/channels/vk/request-permission", json={"channel_id": vk_channel.id})
    assert permission.status_code == 200
    payload = permission.get_json()
    assert payload["ok"] is True
    assert payload["group_id"] == 123456789


def test_booking_payment_message_contains_vk_mini_app_link(monkeypatch):
    monkeypatch.setattr(booking_services, "VK_MINI_APP_APP_ID", "12345")
    monkeypatch.setattr(
        booking_services,
        "_resolve_payment_profile_payload_for_booking",
        lambda db, booking: {
            "recipient_bank": "Т-Банк",
            "recipient_number": "2200123412341234",
            "recipient_full_name": "Тестовый Получатель",
        },
    )
    monkeypatch.setattr(booking_services, "_compute_group_booking_payment_amount", lambda db, booking: 4500)

    booking = type(
        "BookingStub",
        (),
        {
            "id": 65,
            "object_type": "group",
            "group_id": 14,
            "group_start_date": datetime(2026, 4, 1).date(),
        },
    )()

    message = booking_services._build_booking_payment_request_message(None, booking)
    assert "https://vk.com/app12345#context=booking_payment&booking_id=65" in message
    assert "group_id=14" in message
    assert "group_start_date=2026-04-01" in message


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

    response = client.post("/auth/vk", json=_vk_payload(vk_user_id="link-55", name="VK Link User", link_mode=True))
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
    admin_position = next(role for role, spec in ROLES.items() if "manage_staff" in spec.get("permissions", []))
    staff_user = User(name="Manager", telegram_id=50001)
    staff.position = admin_position
    source = User(name="Merge Source")
    target = User(name="Merge Target", requires_manual_merge=True)
    db.add_all([staff_user, source, target])
    db.flush()
    staff.user_id = staff_user.id
    db.add(staff)
    db.commit()
    db.add(UserPhone(user_id=target.id, phone_e164="+79997779991", verified_at=utcnow(), source="sms", is_primary=True))
    db.commit()

    AccountMergeService().try_merge_by_phone(db, user_id=source.id, phone="+79997779991", source="review_test")
    db.commit()
    event = db.query(UserMergeEvent).order_by(UserMergeEvent.id.desc()).first()
    assert event.case_status == "pending_review"

    client = app.test_client()
    _login_by_session(client, db, staff_user.id, telegram_id=staff.telegram_id)

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

