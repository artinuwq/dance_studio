from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOKINGS_SERVICE = ROOT / "src" / "dance_studio" / "web" / "services" / "bookings.py"


def test_booking_admin_notifications_use_telegram_helper():
    source = BOOKINGS_SERVICE.read_text(encoding="utf-8")

    assert 'telegram_api_post("sendMessage", payload, timeout=15)' in source
    assert "failed to notify admin chat" in source


def test_booking_admin_delivery_failure_alert_uses_helper():
    source = BOOKINGS_SERVICE.read_text(encoding="utf-8")

    assert '"sendMessage",' in source
    assert '{"chat_id": admin_chat_id, "text": alert_text}' in source
    assert "failed to send admin delivery-failure alert" in source
