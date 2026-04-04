from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from threading import RLock

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from dance_studio.db.models import AuthIdentity, User, UserPhone


PROVIDERS_WITH_PHONE_MERGE = {"telegram", "vk", "phone"}
_PHONE_LOCKS: dict[str, RLock] = defaultdict(RLock)


class AuthFlowConflictError(RuntimeError):
    pass


class ManualMergeRequiredError(RuntimeError):
    pass


class DuplicateIdentityError(RuntimeError):
    pass


class VerifiedPhoneConflictError(RuntimeError):
    def __init__(self, user_ids: list[int]):
        super().__init__("verified_phone_conflict")
        self.user_ids = user_ids


def _is_identity_provider_unique_violation(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    constraint_name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if constraint_name == "uq_auth_identities_provider_user":
        return True
    message = str(orig or exc).lower()
    return "uq_auth_identities_provider_user" in message


def _normalize_telegram_id(value: int | str | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0:
        return None
    return parsed


def resolve_user_by_telegram(db, telegram_id: int | str | None) -> User | None:
    resolved_id = _normalize_telegram_id(telegram_id)
    if resolved_id is None:
        return None

    user = db.query(User).filter(User.telegram_id == resolved_id).first()
    if user:
        return user

    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == "telegram",
            AuthIdentity.provider_user_id == str(resolved_id),
        )
        .order_by(AuthIdentity.id.desc())
        .first()
    )
    if not identity:
        return None
    return db.query(User).filter(User.id == identity.user_id).first()


def resolve_user_id_by_telegram(db, telegram_id: int | str | None) -> int | None:
    user = resolve_user_by_telegram(db, telegram_id)
    return user.id if user else None


def resolve_telegram_id_by_user(db, user_id: int | str | None) -> int | None:
    resolved_user_id = _normalize_telegram_id(user_id)
    if resolved_user_id is None:
        return None

    user = db.query(User).filter(User.id == resolved_user_id).first()
    if not user:
        return None
    if user.telegram_id:
        return user.telegram_id

    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.user_id == user.id,
            AuthIdentity.provider == "telegram",
        )
        .order_by(AuthIdentity.id.desc())
        .first()
    )
    if not identity or not identity.provider_user_id:
        return None
    return _normalize_telegram_id(identity.provider_user_id)


@contextmanager
def phone_operation_lock(db, phone_e164: str | None):
    if not phone_e164:
        yield
        return
    lock = _PHONE_LOCKS[phone_e164]
    lock.acquire()
    try:
        bind = db.get_bind()
        dialect = getattr(bind.dialect, "name", "")
        if dialect == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:phone))"), {"phone": phone_e164})
        else:
            try:
                db.query(UserPhone).filter(UserPhone.phone_e164 == phone_e164).with_for_update().all()
            except Exception:
                pass
        yield
    finally:
        lock.release()


def normalize_phone_e164(phone: str | None) -> str | None:
    raw = (phone or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    if raw.startswith("+"):
        normalized = f"+{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        normalized = f"+7{digits[1:]}"
    elif len(digits) == 10:
        normalized = f"+7{digits}"
    else:
        normalized = f"+{digits}"
    if len(normalized) < 8:
        return None
    return normalized


def get_verified_phone_user(db, *, phone_e164: str | None, exclude_user_id: int | None = None) -> tuple[User | None, list[int]]:
    if not phone_e164:
        return None, []
    query = db.query(UserPhone).filter(
        UserPhone.phone_e164 == phone_e164,
        UserPhone.verified_at.isnot(None),
    )
    if exclude_user_id is not None:
        query = query.filter(UserPhone.user_id != exclude_user_id)
    rows = query.all()
    user_ids = sorted({row.user_id for row in rows})
    if len(user_ids) != 1:
        return None, user_ids
    user = db.query(User).filter(User.id == user_ids[0], User.is_archived.is_(False)).first()
    return user, user_ids


def set_primary_phone(db, *, user_id: int, phone_row: UserPhone) -> None:
    db.query(UserPhone).filter(UserPhone.user_id == user_id).update({UserPhone.is_primary: False}, synchronize_session=False)
    phone_row.is_primary = True
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.primary_phone = phone_row.phone_e164
        user.phone = phone_row.phone_e164
        user.phone_verified_at = phone_row.verified_at



def ensure_user_phone(
    db,
    *,
    user_id: int,
    phone_e164: str | None,
    source: str,
    verified_at: datetime | None,
    is_primary: bool = False,
) -> UserPhone | None:
    if not phone_e164:
        return None
    with phone_operation_lock(db, phone_e164):
        phone = db.query(UserPhone).filter(UserPhone.user_id == user_id, UserPhone.phone_e164 == phone_e164).first()
        if not phone:
            phone = db.query(UserPhone).filter(UserPhone.phone_e164 == phone_e164).order_by(UserPhone.id.asc()).first()
        if phone and phone.user_id != user_id and phone.verified_at is not None and verified_at is not None:
            raise VerifiedPhoneConflictError([phone.user_id, user_id])
        if not phone:
            phone = UserPhone(
                user_id=user_id,
                phone_e164=phone_e164,
                source=source,
                verified_at=verified_at,
                is_primary=False,
            )
            db.add(phone)
            db.flush()
        else:
            phone.user_id = user_id if phone.user_id == user_id or phone.verified_at is None else phone.user_id
            phone.source = source or phone.source
            phone.verified_at = verified_at or phone.verified_at
        if phone.user_id == user_id and (is_primary or phone.is_primary or verified_at):
            set_primary_phone(db, user_id=user_id, phone_row=phone)
        return phone



def _link_identity_to_user(
    db,
    *,
    user: User,
    provider: str,
    provider_user_id: str | None,
    username: str | None,
    payload_json: str | None,
    verified: bool,
) -> AuthIdentity:
    identity = None
    if provider_user_id:
        identity = (
            db.query(AuthIdentity)
            .filter(AuthIdentity.provider == provider, AuthIdentity.provider_user_id == provider_user_id)
            .with_for_update()
            .first()
        )
    now = datetime.utcnow()
    if identity:
        if identity.user_id not in (None, user.id) and identity.is_verified:
            raise DuplicateIdentityError(f"identity already linked to user {identity.user_id}")
        identity.user_id = user.id
        identity.provider_username = username
        identity.provider_payload_json = payload_json
        identity.is_verified = verified or identity.is_verified
        identity.last_login_at = now
        return identity

    identity = AuthIdentity(
        user_id=user.id,
        provider=provider,
        provider_user_id=provider_user_id,
        provider_username=username,
        provider_payload_json=payload_json,
        linked_at=now,
        last_login_at=now,
        is_primary=False,
        is_verified=verified,
    )
    db.add(identity)
    db.flush()
    return identity



def get_or_create_identity(
    db,
    *,
    provider: str,
    provider_user_id: str | None,
    username: str | None,
    payload_json: str | None,
    fallback_name: str,
    verified_phone: str | None = None,
    current_user_id: int | None = None,
    is_verified: bool = True,
) -> User:
    normalized_phone = normalize_phone_e164(verified_phone)
    with phone_operation_lock(db, normalized_phone):
        identity = None
        if provider_user_id:
            identity = (
                db.query(AuthIdentity)
                .filter(AuthIdentity.provider == provider, AuthIdentity.provider_user_id == provider_user_id)
                .with_for_update()
                .first()
            )
        if identity:
            user = db.query(User).filter(User.id == identity.user_id, User.is_archived.is_(False)).first()
            if user:
                user.last_login_at = datetime.utcnow()
                identity.provider_username = username
                identity.provider_payload_json = payload_json
                identity.last_login_at = user.last_login_at
                if normalized_phone:
                    ensure_user_phone(
                        db,
                        user_id=user.id,
                        phone_e164=normalized_phone,
                        source=provider,
                        verified_at=datetime.utcnow(),
                        is_primary=True,
                    )
                return user

        target_user = None
        if current_user_id:
            target_user = db.query(User).filter(User.id == current_user_id, User.is_archived.is_(False)).first()

        matched_ids: list[int] = []
        if target_user is None and provider in PROVIDERS_WITH_PHONE_MERGE and normalized_phone:
            target_user, matched_ids = get_verified_phone_user(db, phone_e164=normalized_phone)
            if len(matched_ids) > 1:
                raise VerifiedPhoneConflictError(matched_ids)
            if target_user and target_user.requires_manual_merge:
                raise ManualMergeRequiredError("requires_manual_merge")

        created_new_user = target_user is None
        if target_user is None:
            target_user = User(name=fallback_name)

        login_at = datetime.utcnow()
        target_user.last_login_at = login_at
        try:
            with db.begin_nested():
                if created_new_user:
                    db.add(target_user)
                    db.flush()
                _link_identity_to_user(
                    db,
                    user=target_user,
                    provider=provider,
                    provider_user_id=provider_user_id,
                    username=username,
                    payload_json=payload_json,
                    verified=is_verified,
                )
        except IntegrityError as exc:
            if not provider_user_id or not _is_identity_provider_unique_violation(exc):
                raise
            # Concurrent auth requests can race on the same provider_user_id.
            # Reuse the already inserted identity instead of failing the login.
            identity = (
                db.query(AuthIdentity)
                .filter(AuthIdentity.provider == provider, AuthIdentity.provider_user_id == provider_user_id)
                .with_for_update()
                .first()
            )
            if not identity:
                raise
            linked_user = db.query(User).filter(User.id == identity.user_id, User.is_archived.is_(False)).first()
            if not linked_user:
                raise DuplicateIdentityError(f"identity already linked to user {identity.user_id}")
            linked_user.last_login_at = datetime.utcnow()
            identity.provider_username = username
            identity.provider_payload_json = payload_json
            identity.last_login_at = linked_user.last_login_at
            target_user = linked_user

        if normalized_phone:
            ensure_user_phone(
                db,
                user_id=target_user.id,
                phone_e164=normalized_phone,
                source=provider,
                verified_at=datetime.utcnow(),
                is_primary=True,
            )

        target_user._phone_match_user_ids = matched_ids  # type: ignore[attr-defined]
        return target_user
