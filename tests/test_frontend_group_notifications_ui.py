from pathlib import Path


def test_group_admin_ui_uses_neutral_notification_copy():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "ошибка уведомления:" in source
    assert "уведомлено: ${Number(data.group_notification_sent) || 0}/${Number(data.group_notification_total)}" in source
    assert "через подключенные каналы" in source
    assert "часть уведомлений не отправлена" in source
    assert "ошибка TG:" not in source
    assert "В чат группы будет отправлено уведомление" not in source
