from __future__ import annotations

from datetime import datetime, timedelta
import base64
import hashlib
import hmac
import json
import secrets

from dance_studio.core.time import utcnow
from typing import Any
from urllib.parse import urlparse

from dance_studio.auth.services.audit import log_auth_event
from dance_studio.auth.services.contracts import DEFAULT_FALLBACK_AUTH_METHODS
from dance_studio.db.models import PasskeyChallenge, PasskeyCredential

try:
    from fido2.server import Fido2Server
    from fido2.webauthn import (
        AttestationObject,
        AttestedCredentialData,
        AuthenticatorData,
        CollectedClientData,
        PublicKeyCredentialRpEntity,
        PublicKeyCredentialUserEntity,
    )

    FIDO2_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency at runtime
    Fido2Server = None  # type: ignore[assignment]
    AttestationObject = Any  # type: ignore[assignment]
    AttestedCredentialData = Any  # type: ignore[assignment]
    AuthenticatorData = Any  # type: ignore[assignment]
    CollectedClientData = Any  # type: ignore[assignment]
    PublicKeyCredentialRpEntity = Any  # type: ignore[assignment]
    PublicKeyCredentialUserEntity = Any  # type: ignore[assignment]
    FIDO2_AVAILABLE = False


PASSKEY_CHALLENGE_TTL = timedelta(minutes=5)
WEBAUTHN_STORAGE_PREFIX = "webauthn:"
STATE_BYTES_MARKER = "__bytes_b64url__"


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
    now = utcnow()
    db.query(PasskeyChallenge).filter(PasskeyChallenge.expires_at < now).delete(synchronize_session=False)


def _coerce_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if hasattr(value, "items"):
        try:
            return dict(value.items())
        except Exception:
            return None
    try:
        return dict(value)
    except Exception:
        return None


def _webauthn_to_jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _b64url_encode(bytes(value))
    if isinstance(value, (list, tuple, set)):
        return [_webauthn_to_jsonable(item) for item in value]
    mapping = _coerce_mapping(value)
    if mapping is not None:
        normalized: dict[str, Any] = {}
        for key, raw in mapping.items():
            converted = _webauthn_to_jsonable(raw)
            if converted is None:
                continue
            normalized[str(key)] = converted
        return normalized
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)) or enum_value is None:
        if enum_value is not None:
            return enum_value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _state_to_jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {STATE_BYTES_MARKER: _b64url_encode(bytes(value))}
    if isinstance(value, (list, tuple)):
        return [_state_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _state_to_jsonable(v) for k, v in value.items()}
    return value


def _state_from_jsonable(value: Any) -> Any:
    if isinstance(value, list):
        return [_state_from_jsonable(item) for item in value]
    if isinstance(value, dict):
        if set(value.keys()) == {STATE_BYTES_MARKER}:
            raw = value.get(STATE_BYTES_MARKER)
            if isinstance(raw, str):
                try:
                    return _b64url_decode(raw)
                except Exception:
                    return b""
            return b""
        return {k: _state_from_jsonable(v) for k, v in value.items()}
    return value


def _extract_public_key_options(options_payload: dict[str, Any]) -> dict[str, Any]:
    candidate = options_payload.get("publicKey")
    if isinstance(candidate, dict):
        return candidate
    return options_payload


def _sanitize_webauthn_options_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_webauthn_options_payload(item) for item in value]
    mapping = _coerce_mapping(value)
    if mapping is None:
        return value
    normalized: dict[str, Any] = {}
    for key, raw in mapping.items():
        key_str = str(key)
        # Some browsers reject this extension member when passed from options.
        if key_str == "sameOriginWithAncestors":
            continue
        cleaned = _sanitize_webauthn_options_payload(raw)
        if cleaned is None:
            continue
        normalized[key_str] = cleaned
    return normalized


def _extract_challenge_from_options(options_payload: dict[str, Any]) -> str:
    public_key_options = _extract_public_key_options(options_payload)
    return str(public_key_options.get("challenge") or "").strip()


def _load_challenge_payload(challenge: PasskeyChallenge | None) -> dict[str, Any]:
    if not challenge or not challenge.payload_json:
        return {}
    try:
        payload = json.loads(challenge.payload_json)
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _build_fido2_server(*, origin: str, rp_id: str):
    if not FIDO2_AVAILABLE or Fido2Server is None:
        return None
    normalized_origin = _normalize_origin(origin)
    if not normalized_origin or not rp_id:
        return None
    rp = PublicKeyCredentialRpEntity(id=rp_id, name="Dance Studio")

    def _verify_origin(candidate_origin: str) -> bool:
        return _normalize_origin(candidate_origin) == normalized_origin

    try:
        return Fido2Server(rp, attestation="none", verify_origin=_verify_origin)
    except TypeError:
        try:
            return Fido2Server(rp, verify_origin=_verify_origin)
        except TypeError:
            return Fido2Server(rp)


def _decode_webauthn_credential_data(storage_value: str | None):
    raw = str(storage_value or "").strip()
    if not raw.startswith(WEBAUTHN_STORAGE_PREFIX):
        return None
    encoded = raw[len(WEBAUTHN_STORAGE_PREFIX) :].strip()
    if not encoded or not FIDO2_AVAILABLE:
        return None
    try:
        return AttestedCredentialData(_b64url_decode(encoded))
    except Exception:
        return None


def _decode_json_blob_from_b64url(value: str) -> dict[str, Any] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(_b64url_decode(raw).decode("utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _is_legacy_attestation_payload(attestation_object_raw: str) -> bool:
    decoded = _decode_json_blob_from_b64url(attestation_object_raw)
    if not decoded:
        return False
    return bool(decoded.get("credentialId") or decoded.get("publicKey"))


def _is_legacy_authenticator_payload(authenticator_data_b64: str) -> bool:
    decoded = _decode_json_blob_from_b64url(authenticator_data_b64)
    if not decoded:
        return False
    return "rpId" in decoded or "signCount" in decoded


def _candidate_credential_ids(credential: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for candidate in (
        credential.get("id"),
        credential.get("rawId"),
        payload.get("credential_id"),
        payload.get("raw_id"),
    ):
        value = str(candidate or "").strip()
        if not value or value in result:
            continue
        result.append(value)
        try:
            normalized = _b64url_encode(_b64url_decode(value))
        except Exception:
            normalized = ""
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _credential_id_to_bytes(credential: dict[str, Any], payload: dict[str, Any]) -> bytes:
    raw_id = str(credential.get("rawId") or payload.get("raw_id") or "").strip()
    if raw_id:
        try:
            return _b64url_decode(raw_id)
        except Exception:
            return raw_id.encode("utf-8")
    credential_id = str(credential.get("id") or payload.get("credential_id") or "").strip()
    if not credential_id:
        return b""
    try:
        return _b64url_decode(credential_id)
    except Exception:
        return credential_id.encode("utf-8")


def _has_invalid_sign_count(next_sign_count: int, previous_sign_count: int) -> bool:
    next_value = max(int(next_sign_count or 0), 0)
    previous_value = max(int(previous_sign_count or 0), 0)
    # Some synced passkeys always return 0; treat 0->0 as valid.
    if next_value == 0 and previous_value == 0:
        return False
    return next_value <= previous_value


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
        challenge_value: str | None = None,
    ) -> PasskeyChallenge:
        _cleanup_expired_challenges(db)
        challenge = PasskeyChallenge(
            challenge=(challenge_value or _b64url_encode(secrets.token_bytes(32))),
            flow_type=flow_type,
            user_id=user_id,
            session_user_id=session_user_id,
            origin=origin,
            rp_id=rp_id,
            expires_at=utcnow() + PASSKEY_CHALLENGE_TTL,
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
            PasskeyChallenge.expires_at >= utcnow(),
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
        server = _build_fido2_server(origin=origin, rp_id=rp_id)
        if server:
            existing_rows = db.query(PasskeyCredential).filter(PasskeyCredential.user_id == user_id).all()
            existing_credentials = []
            for row in existing_rows:
                parsed = _decode_webauthn_credential_data(row.public_key)
                if parsed is not None:
                    existing_credentials.append(parsed)
            user_entity = PublicKeyCredentialUserEntity(
                id=str(user_id).encode("utf-8"),
                name=f"user-{user_id}",
                display_name=f"user-{user_id}",
            )
            try:
                options, state = server.register_begin(
                    user_entity,
                    credentials=(existing_credentials or None),
                    user_verification="preferred",
                    resident_key_requirement="preferred",
                )
            except TypeError:
                try:
                    options, state = server.register_begin(
                        user_entity,
                        credentials=(existing_credentials or None),
                        user_verification="preferred",
                    )
                except TypeError:
                    options, state = server.register_begin(
                        user_entity,
                        credentials=(existing_credentials or None),
                    )
            options_payload = _sanitize_webauthn_options_payload(_webauthn_to_jsonable(options))
            if not isinstance(options_payload, dict):
                options_payload = {}
            public_key_options = _extract_public_key_options(options_payload)
            challenge_value = _extract_challenge_from_options(options_payload)
            if challenge_value:
                self._create_challenge(
                    db,
                    flow_type="register",
                    user_id=user_id,
                    session_user_id=session_user_id,
                    origin=origin,
                    rp_id=rp_id,
                    challenge_value=challenge_value,
                    payload={
                        "mode": "webauthn",
                        "state": _state_to_jsonable(state),
                    },
                )
                return {
                    "status": "ok",
                    "publicKey": public_key_options,
                    "challenge": challenge_value,
                    "rp_id": rp_id,
                    "origin": origin,
                    "fallback_auth_methods": DEFAULT_FALLBACK_AUTH_METHODS,
                }

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

    def _register_complete_webauthn(
        self,
        db,
        *,
        user_id: int,
        payload: dict,
        challenge: PasskeyChallenge,
        client_data_json: str,
        attestation_object_raw: str,
        origin: str,
        rp_id: str,
    ):
        server = _build_fido2_server(origin=challenge.origin, rp_id=challenge.rp_id)
        if not server:
            return None, "passkey_webauthn_unavailable"
        challenge_payload = _load_challenge_payload(challenge)
        state = _state_from_jsonable(challenge_payload.get("state", {}))
        try:
            client_data_obj = CollectedClientData(_b64url_decode(client_data_json))
            attestation_obj = AttestationObject(_b64url_decode(attestation_object_raw))
            auth_data = server.register_complete(state, client_data_obj, attestation_obj)
        except Exception:
            return None, "invalid_passkey_attestation"

        credential_data = getattr(auth_data, "credential_data", None)
        if credential_data is None:
            return None, "invalid_attestation_object"
        credential_bytes = bytes(credential_data)
        credential_id_bytes = bytes(getattr(credential_data, "credential_id", b""))
        if not credential_bytes or not credential_id_bytes:
            return None, "invalid_attestation_object"
        credential_id = _b64url_encode(credential_id_bytes)

        existing = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == credential_id).first()
        if existing:
            if existing.user_id != user_id:
                return None, "credential_already_registered"
            return None, "duplicate_passkey"

        response = (payload.get("credential") or {}).get("response") or {}
        transports_raw = response.get("transports") or payload.get("transports") or []
        if isinstance(transports_raw, str):
            transports_list = [item.strip() for item in transports_raw.split(",") if item.strip()]
        elif isinstance(transports_raw, list):
            transports_list = [str(item).strip() for item in transports_raw if str(item).strip()]
        else:
            transports_list = []
        sign_count = int(getattr(auth_data, "counter", 0) or 0)
        challenge.used_at = utcnow()
        registered = PasskeyCredential(
            user_id=user_id,
            credential_id=credential_id,
            public_key=f"{WEBAUTHN_STORAGE_PREFIX}{_b64url_encode(credential_bytes)}",
            sign_count=max(sign_count, 0),
            transports=",".join(transports_list),
            device_name=str(payload.get("device_name") or "Passkey").strip() or "Passkey",
            created_at=utcnow(),
        )
        db.add(registered)
        log_auth_event(
            db,
            event_type="passkey_register",
            provider=self.provider_name,
            user_id=user_id,
            payload={"credential_id": credential_id, "origin": origin, "rp_id": rp_id, "mode": "webauthn"},
        )
        return registered, None

    def _register_complete_legacy(
        self,
        db,
        *,
        user_id: int,
        payload: dict,
        challenge: PasskeyChallenge,
        attestation_object_raw: str,
        origin: str,
        rp_id: str,
    ):
        try:
            attestation = _json_from_b64url(attestation_object_raw)
        except ValueError as exc:
            return None, str(exc)

        if str(attestation.get("rpId") or "").strip().lower() != str(challenge.rp_id).lower():
            return None, "invalid_passkey_rp_id"

        credential = payload.get("credential") or {}
        credential_id = str(attestation.get("credentialId") or credential.get("id") or "").strip()
        public_key = str(attestation.get("publicKey") or "").strip()
        if not credential_id or not public_key:
            return None, "invalid_attestation_object"

        existing = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == credential_id).first()
        if existing:
            if existing.user_id != user_id:
                return None, "credential_already_registered"
            return None, "duplicate_passkey"

        challenge.used_at = utcnow()
        registered = PasskeyCredential(
            user_id=user_id,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=int(attestation.get("signCount") or 0),
            transports=",".join(attestation.get("transports") or payload.get("transports") or []),
            device_name=str(attestation.get("deviceName") or payload.get("device_name") or "Passkey").strip() or "Passkey",
            created_at=utcnow(),
        )
        db.add(registered)
        log_auth_event(
            db,
            event_type="passkey_register",
            provider=self.provider_name,
            user_id=user_id,
            payload={"credential_id": credential_id, "origin": origin, "rp_id": rp_id, "mode": "legacy"},
        )
        return registered, None

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
        except ValueError as exc:
            return None, str(exc)

        if not _is_legacy_attestation_payload(attestation_object_raw):
            credential_row, error = self._register_complete_webauthn(
                db,
                user_id=user_id,
                payload=payload,
                challenge=challenge,
                client_data_json=client_data_json,
                attestation_object_raw=attestation_object_raw,
                origin=origin,
                rp_id=rp_id,
            )
            if not error:
                return credential_row, None
            if error != "passkey_webauthn_unavailable":
                return None, error

        return self._register_complete_legacy(
            db,
            user_id=user_id,
            payload=payload,
            challenge=challenge,
            attestation_object_raw=attestation_object_raw,
            origin=origin,
            rp_id=rp_id,
        )

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
        server = _build_fido2_server(origin=origin, rp_id=rp_id)
        if server:
            try:
                options, state = server.authenticate_begin(user_verification="preferred")
            except TypeError:
                options, state = server.authenticate_begin()
            options_payload = _sanitize_webauthn_options_payload(_webauthn_to_jsonable(options))
            if not isinstance(options_payload, dict):
                options_payload = {}
            public_key_options = _extract_public_key_options(options_payload)
            challenge_value = _extract_challenge_from_options(options_payload)
            if challenge_value:
                self._create_challenge(
                    db,
                    flow_type="login",
                    user_id=None,
                    session_user_id=None,
                    origin=origin,
                    rp_id=rp_id,
                    challenge_value=challenge_value,
                    payload={
                        "mode": "webauthn",
                        "state": _state_to_jsonable(state),
                    },
                )
                return {
                    "status": "ok",
                    "publicKey": public_key_options,
                    "challenge": challenge_value,
                    "rp_id": rp_id,
                    "origin": origin,
                    "fallback_auth_methods": DEFAULT_FALLBACK_AUTH_METHODS,
                }

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

    def _login_complete_webauthn(
        self,
        db,
        *,
        payload: dict,
        credential: dict,
        challenge: PasskeyChallenge,
        stored: PasskeyCredential,
        client_data_json: str,
        authenticator_data_b64: str,
        signature: str,
        origin: str,
        rp_id: str,
    ):
        server = _build_fido2_server(origin=challenge.origin, rp_id=challenge.rp_id)
        if not server:
            return None, "passkey_webauthn_unavailable"
        stored_credential_data = _decode_webauthn_credential_data(stored.public_key)
        if stored_credential_data is None:
            return None, "credential_not_found"
        challenge_payload = _load_challenge_payload(challenge)
        state = _state_from_jsonable(challenge_payload.get("state", {}))

        credential_id_bytes = _credential_id_to_bytes(credential, payload)
        if not credential_id_bytes:
            credential_id_bytes = bytes(getattr(stored_credential_data, "credential_id", b""))
        try:
            client_data_obj = CollectedClientData(_b64url_decode(client_data_json))
            auth_data_obj = AuthenticatorData(_b64url_decode(authenticator_data_b64))
            signature_bytes = _b64url_decode(signature)
            server.authenticate_complete(
                state,
                [stored_credential_data],
                credential_id_bytes,
                client_data_obj,
                auth_data_obj,
                signature_bytes,
            )
        except Exception as exc:
            message = str(exc).lower()
            if "counter" in message or "sign count" in message:
                return None, "invalid_sign_count"
            return None, "invalid_passkey_signature"

        previous_sign_count = int(stored.sign_count or 0)
        next_sign_count = int(getattr(auth_data_obj, "counter", 0) or 0)
        if _has_invalid_sign_count(next_sign_count, previous_sign_count):
            return None, "invalid_sign_count"

        stored.sign_count = next_sign_count
        stored.last_used_at = utcnow()
        challenge.used_at = utcnow()
        log_auth_event(
            db,
            event_type="passkey_login",
            provider=self.provider_name,
            user_id=stored.user_id,
            payload={"credential_id": stored.credential_id, "origin": origin, "rp_id": rp_id, "mode": "webauthn"},
        )
        return stored, None

    def _login_complete_legacy(
        self,
        db,
        *,
        challenge: PasskeyChallenge,
        stored: PasskeyCredential,
        client_data_json: str,
        authenticator_data_b64: str,
        signature: str,
        origin: str,
        rp_id: str,
    ):
        try:
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

        previous_sign_count = int(stored.sign_count or 0)
        next_sign_count = int(auth_data.get("signCount") or 0)
        if _has_invalid_sign_count(next_sign_count, previous_sign_count):
            return None, "invalid_sign_count"

        stored.sign_count = next_sign_count
        stored.last_used_at = utcnow()
        challenge.used_at = utcnow()
        log_auth_event(
            db,
            event_type="passkey_login",
            provider=self.provider_name,
            user_id=stored.user_id,
            payload={"credential_id": stored.credential_id, "origin": origin, "rp_id": rp_id, "mode": "legacy"},
        )
        return stored, None

    def login_complete(self, db, payload: dict, *, origin: str, rp_id: str):
        credential = payload.get("credential") or {}
        response = credential.get("response") or {}
        client_data_json = str(response.get("clientDataJSON") or payload.get("client_data_json") or "").strip()
        authenticator_data_b64 = str(response.get("authenticatorData") or payload.get("authenticator_data") or "").strip()
        signature = str(response.get("signature") or payload.get("signature") or "").strip()
        if not client_data_json or not authenticator_data_b64 or not signature:
            return None, "passkey_login_payload_required"

        candidate_ids = _candidate_credential_ids(credential, payload)
        if not candidate_ids:
            return None, "credential_not_found"
        stored = None
        for candidate_id in candidate_ids:
            stored = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == candidate_id).first()
            if stored:
                break
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
        except ValueError as exc:
            return None, str(exc)

        is_webauthn_stored = str(stored.public_key or "").startswith(WEBAUTHN_STORAGE_PREFIX)
        if is_webauthn_stored and not _is_legacy_authenticator_payload(authenticator_data_b64):
            credential_row, error = self._login_complete_webauthn(
                db,
                payload=payload,
                credential=credential,
                challenge=challenge,
                stored=stored,
                client_data_json=client_data_json,
                authenticator_data_b64=authenticator_data_b64,
                signature=signature,
                origin=origin,
                rp_id=rp_id,
            )
            if not error:
                return credential_row, None
            if error != "passkey_webauthn_unavailable":
                return None, error

        return self._login_complete_legacy(
            db,
            challenge=challenge,
            stored=stored,
            client_data_json=client_data_json,
            authenticator_data_b64=authenticator_data_b64,
            signature=signature,
            origin=origin,
            rp_id=rp_id,
        )

    @staticmethod
    def resolve_origin_and_rp_id(*, origin: str | None, rp_id: str | None = None) -> tuple[str | None, str | None]:
        normalized_origin = _normalize_origin(origin)
        return normalized_origin, _resolve_rp_id(normalized_origin, rp_id)

