from __future__ import annotations


def direction_upload_session_validation_error(session, telegram_user_id: int | None) -> str | None:
    if not session:
        return "❌ Токен не найден. Проверьте, что ссылка скопирована правильно."

    if telegram_user_id is None:
        return "❌ Не удалось определить Telegram-пользователя."

    expected_user_id = getattr(session, "telegram_user_id", None)
    if expected_user_id is not None and expected_user_id != telegram_user_id:
        return "❌ Этот токен привязан к другому Telegram-пользователю."

    status = getattr(session, "status", None)
    if status != "waiting_for_photo":
        return f"❌ Сессия уже в процессе обработки (статус: {status})"

    return None
