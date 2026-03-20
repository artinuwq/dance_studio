from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

from dance_studio.auth.services.audit import log_auth_event
from dance_studio.auth.services.contracts import DEFAULT_FALLBACK_AUTH_METHODS
from dance_studio.db.models import PasskeyChallenge, PasskeyCredential


PASSKEY_CHALLENGE_TTL = timedelta(minutes=5)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _json_from_b64url(value: str) -> dict:
    try:
        return json.loads(_b64url_decode(value).decode("utf-8"))
    except Exception as exc:  # pragma: no cover - guarded by tests via error path
        raise ValueError("invalid_client_data") from exc


def _normalize_origin(origin: str | None) -> str | None:
    raw = (origin or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _resolve_rp_id(origin: str | None, rp_id: str | None = None) -> str | None:
    if rp_id:
        return str(rp_id).strip().lower() or None
    normalized_origin = _normalize_origin(origin)
    if not normalized_origin:
        return None
    return urlparse(normalized_origin).hostname


def _cleanup_expired_challenges(db) -> None:
    now = datetime.utcnow()
    db.query(PasskeyChallenge).filter(PasskeyChallenge.expires_at < now).delete(synchronize_session=False)


class PasskeyAuthProvider:
    provider_name = "passkey"

    def _create_challenge(
        self,
        db,
        *,
        flow_type: str,
        user_id: int | None,
        session_user_id: int | None,
        origin: str,
        rp_id: str,
        payload: dict | None = None,
    ) -> PasskeyChallenge:
        _cleanup_expired_challenges(db)
        challenge_value = _b64url_encode(secrets.token_bytes(32))
        challenge = PasskeyChallenge(
            challenge=challenge_value,
            flow_type=flow_type,
            user_id=user_id,
            session_user_id=session_user_id,
            origin=origin,
            rp_id=rp_id,
            expires_at=datetime.utcnow() + PASSKEY_CHALLENGE_TTL,
            payload_json=(json.dumps(payload, ensure_ascii=False) if payload is not None else None),
        )
        db.add(challenge)
        db.flush()
        return challenge

    def _get_active_challenge(
        self,
        db,
        *,
        flow_type: str,
        challenge_value: str,
        user_id: int | None = None,
    ) -> PasskeyChallenge | None:
        _cleanup_expired_challenges(db)
        query = db.query(PasskeyChallenge).filter(
            PasskeyChallenge.flow_type == flow_type,
            PasskeyChallenge.challenge == challenge_value,
            PasskeyChallenge.used_at.is_(None),
            PasskeyChallenge.expires_at >= datetime.utcnow(),
        )
        if user_id is not None:
            query = query.filter(PasskeyChallenge.user_id == user_id)
        return query.order_by(PasskeyChallenge.id.desc()).first()

    def _validate_client_data(
        self,
        *,
        client_data_json_b64: str,
        expected_type: str,
        expected_origin: str,
        expected_challenge: str,
    ) -> dict:
        client_data = _json_from_b64url(client_data_json_b64)
        if client_data.get("type") != expected_type:
            raise ValueError("invalid_client_data_type")
        if client_data.get("challenge") != expected_challenge:
            raise ValueError("invalid_passkey_challenge")
        if _normalize_origin(client_data.get("origin")) != _normalize_origin(expected_origin):
            raise ValueError("invalid_passkey_origin")
        return client_data

    def register_begin(self, db, *, user_id: int, session_user_id: int | None, origin: str, rp_id: str):
        challenge = self._create_challenge(
            db,
            flow_type="register",
            user_id=user_id,
            session_user_id=session_user_id,
            origin=origin,
            rp_id=rp_id,
        )
        return {
            "status": "ok",
            "publicKey": {
                "challenge": challenge.challenge,
                "rp": {"id": rp_id, "name": "Dance Studio"},
                "user": {
                    "id": _b64url_encode(str(user_id).encode("utf-8")),
                    "name": f"user-{user_id}",
                    "displayName": f"user-{user_id}",
                },
                "pubKeyCredParams": [{"alg": -7, "type": "public-key"}],
                "timeout": int(PASSKEY_CHALLENGE_TTL.total_seconds() * 1000),
                "attestation": "none",
                "authenticatorSelection": {"userVerification": "preferred"},
            },
            "challenge": challenge.challenge,
            "rp_id": rp_id,
            "origin": origin,
            "fallback_auth_methods": DEFAULT_FALLBACK_AUTH_METHODS,
        }

    def register_complete(self, db, *, user_id: int, payload: dict, origin: str, rp_id: str):
        if not user_id:
            return None, "auth_required"
        credential = payload.get("credential") or {}
        response = credential.get("response") or {}
        client_data_json = str(response.get("clientDataJSON") or payload.get("client_data_json") or "").strip()
        attestation_object_raw = str(response.get("attestationObject") or payload.get("attestation_object") or "").strip()
        if not client_data_json or not attestation_object_raw:
            return None, "passkey_registration_payload_required"

        try:
            client_data = _json_from_b64url(client_data_json)
            challenge_value = str(client_data.get("challenge") or "")
            challenge = self._get_active_challenge(db, flow_type="register", challenge_value=challenge_value, user_id=user_id)
            if not challenge:
                return None, "invalid_passkey_challenge"
            self._validate_client_data(
                client_data_json_b64=client_data_json,
                expected_type="webauthn.create",
                expected_origin=challenge.origin,
                expected_challenge=challenge.challenge,
            )
            attestation = _json_from_b64url(attestation_object_raw)
        except ValueError as exc:
            return None, str(exc)

        if str(attestation.get("rpId") or "").strip().lower() != str(challenge.rp_id).lower():
            return None, "invalid_passkey_rp_id"

        credential_id = str(attestation.get("credentialId") or credential.get("id") or "").strip()
        public_key = str(attestation.get("publicKey") or "").strip()
        if not credential_id or not public_key:
            return None, "invalid_attestation_object"

        existing = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == credential_id).first()
        if existing:
            if existing.user_id != user_id:
                return None, "credential_already_registered"
            return None, "duplicate_passkey"

        challenge.used_at = datetime.utcnow()
        registered = PasskeyCredential(
            user_id=user_id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=int(attestation.get("signCount") or 0),
            transports=",".join(attestation.get("transports") or payload.get("transports") or []),
            device_name=str(attestation.get("deviceName") or payload.get("device_name") or "Passkey").strip() or "Passkey",
            created_at=datetime.utcnow(),
        )
        db.add(registered)
        log_auth_event(
            db,
            event_type="passkey_register",
            provider=self.provider_name,
            user_id=user_id,
            payload={"credential_id": credential_id, "origin": origin, "rp_id": rp_id},
        )
        return registered, None

    def list_credentials(self, db, *, user_id: int) -> list[PasskeyCredential]:
        return (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.user_id == user_id)
            .order_by(PasskeyCredential.created_at.asc(), PasskeyCredential.id.asc())
            .all()
        )

    def delete_credential(self, db, *, user_id: int, credential_id: str):
        credential = (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.user_id == user_id, PasskeyCredential.credential_id == credential_id)
            .first()
        )
        if not credential:
            return False
        db.delete(credential)
        log_auth_event(
            db,
            event_type="passkey_delete",
            provider=self.provider_name,
            user_id=user_id,
            payload={"credential_id": credential_id},
        )
        return True

    def login_begin(self, db, *, origin: str, rp_id: str):
        challenge = self._create_challenge(
            db,
            flow_type="login",
            user_id=None,
            session_user_id=None,
            origin=origin,
            rp_id=rp_id,
        )
        return {
            "status": "ok",
            "publicKey": {
                "challenge": challenge.challenge,
                "rpId": rp_id,
                "timeout": int(PASSKEY_CHALLENGE_TTL.total_seconds() * 1000),
                "userVerification": "preferred",
                "allowCredentials": [],
            },
            "challenge": challenge.challenge,
            "rp_id": rp_id,
            "origin": origin,
            "fallback_auth_methods": DEFAULT_FALLBACK_AUTH_METHODS,
        }

    def login_complete(self, db, payload: dict, *, origin: str, rp_id: str):
        credential = payload.get("credential") or {}
        response = credential.get("response") or {}
        credential_id = str(credential.get("id") or payload.get("credential_id") or "").strip()
        client_data_json = str(response.get("clientDataJSON") or payload.get("client_data_json") or "").strip()
        authenticator_data_b64 = str(response.get("authenticatorData") or payload.get("authenticator_data") or "").strip()
        signature = str(response.get("signature") or payload.get("signature") or "").strip()
        if not credential_id or not client_data_json or not authenticator_data_b64 or not signature:
            return None, "passkey_login_payload_required"

        stored = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == credential_id).first()
        if not stored:
            return None, "credential_not_found"
        try:
            client_data = _json_from_b64url(client_data_json)
            challenge_value = str(client_data.get("challenge") or "")
            challenge = self._get_active_challenge(db, flow_type="login", challenge_value=challenge_value)
            if not challenge:
                return None, "invalid_passkey_challenge"
            self._validate_client_data(
                client_data_json_b64=client_data_json,
                expected_type="webauthn.get",
                expected_origin=challenge.origin,
                expected_challenge=challenge.challenge,
            )
            auth_data = _json_from_b64url(authenticator_data_b64)
        except ValueError as exc:
            return None, str(exc)

        if str(auth_data.get("rpId") or "").strip().lower() != str(challenge.rp_id).lower():
            return None, "invalid_passkey_rp_id"
        if not bool(auth_data.get("userPresent", True)):
            return None, "user_presence_required"

        expected_signature = _b64url_encode(
            hmac.new(
                stored.public_key.encode("utf-8"),
                f"{authenticator_data_b64}.{client_data_json}".encode("utf-8"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature, expected_signature):
            return None, "invalid_passkey_signature"

        next_sign_count = int(auth_data.get("signCount") or 0)
        if next_sign_count <= int(stored.sign_count or 0):
            return None, "invalid_sign_count"

        stored.sign_count = next_sign_count
        stored.last_used_at = datetime.utcnow()
        challenge.used_at = datetime.utcnow()
        log_auth_event(
            db,
            event_type="passkey_login",
            provider=self.provider_name,
            user_id=stored.user_id,
            payload={"credential_id": credential_id, "origin": origin, "rp_id": rp_id},
        )
        return stored, None

    @staticmethod
    def resolve_origin_and_rp_id(*, origin: str | None, rp_id: str | None = None) -> tuple[str | None, str | None]:
        normalized_origin = _normalize_origin(origin)
        return normalized_origin, _resolve_rp_id(normalized_origin, rp_id)
