from types import SimpleNamespace

from dance_studio.bot.upload_sessions import direction_upload_session_validation_error


def test_direction_upload_session_validation_accepts_owner_with_waiting_status():
    session = SimpleNamespace(telegram_user_id=123, status="waiting_for_photo")

    assert direction_upload_session_validation_error(session, 123) is None


def test_direction_upload_session_validation_rejects_foreign_telegram_user():
    session = SimpleNamespace(telegram_user_id=123, status="waiting_for_photo")

    error = direction_upload_session_validation_error(session, 999)

    assert error == "❌ Этот токен привязан к другому Telegram-пользователю."


def test_direction_upload_session_validation_rejects_non_waiting_status():
    session = SimpleNamespace(telegram_user_id=123, status="completed")

    error = direction_upload_session_validation_error(session, 123)

    assert error == "❌ Сессия уже в процессе обработки (статус: completed)"
