from pathlib import Path


def test_bot_exposes_command_to_bind_current_chat_for_booking_notifications():
    source = Path("src/dance_studio/bot/bot.py").read_text(encoding="utf-8")

    assert 'Command("set_bookings_admin_chat")' in source
    assert 'BOOKINGS_ADMIN_CHAT_ID_SETTING_KEY' in source
    assert 'update_setting(' in source
    assert 'key=BOOKINGS_ADMIN_CHAT_ID_SETTING_KEY' in source
    assert 'has_permission(position, "manage_schedule")' in source
    assert 'Эту команду нужно запускать в группе или супергруппе' in source
    assert 'Нет доступа к настройке чата заявок.' in source
    assert 'Чат для уведомлений по заявкам обновлен.' in source
