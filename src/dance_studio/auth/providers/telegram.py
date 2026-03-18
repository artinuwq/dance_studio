from __future__ import annotations

import json

from dance_studio.auth.services.common import get_or_create_identity
from dance_studio.core.tg_auth import validate_init_data
from dance_studio.db.models import User


class TelegramAuthProvider:
    provider_name = "telegram"

    def authenticate(self, db, init_data: str):
        verified = validate_init_data(init_data)
        if not verified:
            return None, "invalid_init_data"
        user = get_or_create_identity(
            db,
            provider=self.provider_name,
            provider_user_id=str(verified.user_id),
            username=None,
            payload_json=json.dumps({"replay_key": verified.replay_key}, ensure_ascii=False),
            fallback_name=f"Telegram {verified.user_id}",
        )
        existing = (
            db.query(User)
            .filter(User.telegram_id == verified.user_id)
            .first()
        )
        if existing and existing.id != user.id:
            return existing, None
        if user.telegram_id is None:
            user.telegram_id = verified.user_id
        return user, None
