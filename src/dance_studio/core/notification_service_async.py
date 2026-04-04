from __future__ import annotations

import asyncio
from typing import Any

from dance_studio.core.notification_service import send_user_notification_sync


async def send_user_notification_async(
    bot,
    user_id: int,
    text: str,
    context_note: str = "User notification",
    parse_mode: str = "HTML",
    reply_markup: Any = None,
) -> bool:
    """Async compatibility wrapper for legacy call sites."""
    # `bot` is intentionally unused: legacy callers still pass it, but the
    # unified notification flow now uses provider-based routing.
    _ = bot
    return await asyncio.to_thread(
        send_user_notification_sync,
        user_id,
        text,
        context_note,
        parse_mode,
        reply_markup,
    )

