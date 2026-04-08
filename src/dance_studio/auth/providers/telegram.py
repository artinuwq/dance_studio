from __future__ import annotations

import json

from dance_studio.auth.services.account_merge import AccountMergeService
from dance_studio.auth.services.common import get_or_create_identity, normalize_phone_e164
from dance_studio.core.tg_auth import validate_init_data
from dance_studio.db.models import AuthIdentity, User


def _normalize_profile_value(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _build_telegram_display_name(verified) -> str | None:
    first_name = _normalize_profile_value(getattr(verified, "first_name", None))
    last_name = _normalize_profile_value(getattr(verified, "last_name", None))
    if first_name or last_name:
        return " ".join(part for part in [first_name, last_name] if part)
    return None


def _is_generated_telegram_name(name: str | None, telegram_user_id: int) -> bool:
    normalized = _normalize_profile_value(name)
    if not normalized:
        return True
    return normalized == f"Telegram {telegram_user_id}"


class TelegramAuthProvider:
    provider_name = "telegram"

    def authenticate(self, db, init_data: str, *, current_user_id: int | None = None, verified_phone: str | None = None):
        verified = validate_init_data(init_data)
        if not verified:
            return None, "invalid_init_data"
        telegram_name = _build_telegram_display_name(verified)
        telegram_username = _normalize_profile_value(getattr(verified, "username", None))
        fallback_name = telegram_name or f"Telegram {verified.user_id}"

        # Legacy users can still have telegram_id without auth_identities row.
        # Reuse that user as a target to avoid creating a parallel duplicate account.
        existing_user = (
            db.query(User)
            .filter(User.telegram_id == verified.user_id, User.is_archived.is_(False))
            .order_by(User.id.asc())
            .first()
        )
        if existing_user:
            if telegram_username:
                existing_user.username = telegram_username
            if telegram_name and _is_generated_telegram_name(existing_user.name, verified.user_id):
                existing_user.name = telegram_name
            identity = (
                db.query(AuthIdentity)
                .filter(
                    AuthIdentity.provider == self.provider_name,
                    AuthIdentity.provider_user_id == str(verified.user_id),
                )
                .order_by(AuthIdentity.id.asc())
                .first()
            )
            if identity and identity.user_id != existing_user.id:
                if current_user_id is None:
                    # Normal Telegram login: trust Telegram ID ownership and
                    # rebind stale identity links caused by old link flow.
                    identity.user_id = existing_user.id
                else:
                    can_reconcile = current_user_id in (existing_user.id, identity.user_id)
                    conflict_user = (
                        db.query(User)
                        .filter(User.id == identity.user_id, User.is_archived.is_(False))
                        .first()
                        if can_reconcile
                        else None
                    )
                    if conflict_user:
                        primary_id, _ = AccountMergeService().merge_users(
                            db,
                            user_a_id=existing_user.id,
                            user_b_id=conflict_user.id,
                            reason="telegram_identity_reconcile",
                            strategy="auto_by_telegram_id",
                        )
                        existing_user = (
                            db.query(User)
                            .filter(User.id == primary_id, User.is_archived.is_(False))
                            .first()
                        )
                    elif can_reconcile:
                        identity.user_id = existing_user.id
        elif current_user_id is None:
            # Recovery path for previously mislinked Telegram identities:
            # if this telegram id is linked to a user with different primary
            # telegram_id, detach it into its own canonical user.
            identity = (
                db.query(AuthIdentity)
                .filter(
                    AuthIdentity.provider == self.provider_name,
                    AuthIdentity.provider_user_id == str(verified.user_id),
                )
                .order_by(AuthIdentity.id.asc())
                .first()
            )
            if identity:
                identity_user = (
                    db.query(User)
                    .filter(User.id == identity.user_id, User.is_archived.is_(False))
                    .first()
                )
                if identity_user and identity_user.telegram_id not in (None, verified.user_id):
                    existing_user = User(
                        name=fallback_name,
                        telegram_id=verified.user_id,
                        username=telegram_username,
                    )
                    db.add(existing_user)
                    db.flush()
                    identity.user_id = existing_user.id

        target_user_id = current_user_id or (existing_user.id if existing_user else None)
        normalized_phone = normalize_phone_e164(verified_phone)
        user = get_or_create_identity(
            db,
            provider=self.provider_name,
            provider_user_id=str(verified.user_id),
            username=telegram_username,
            payload_json=json.dumps({"replay_key": verified.replay_key}, ensure_ascii=False),
            fallback_name=fallback_name,
            verified_phone=normalized_phone,
            current_user_id=target_user_id,
        )
        if user.telegram_id is None:
            user.telegram_id = verified.user_id
        if telegram_username:
            user.username = telegram_username
        if telegram_name and _is_generated_telegram_name(user.name, verified.user_id):
            user.name = telegram_name
        return user, None
