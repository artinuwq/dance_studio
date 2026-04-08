from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USERBOT = ROOT / "src" / "dance_studio" / "bot" / "telegram_userbot.py"
STARTUP_STATUS = ROOT / "src" / "dance_studio" / "bot" / "startup_status.py"


def test_userbot_client_disables_extra_telethon_retries():
    source = USERBOT.read_text(encoding="utf-8")

    assert "USERBOT_REQUEST_RETRIES = 1" in source
    assert "USERBOT_CONNECTION_RETRIES = 1" in source
    assert "USERBOT_RETRY_DELAY_SECONDS = 0" in source
    assert "auto_reconnect=False" in source


def test_startup_status_probe_uses_reduced_retry_settings():
    source = STARTUP_STATUS.read_text(encoding="utf-8")

    assert "request_retries=USERBOT_REQUEST_RETRIES" in source
    assert "connection_retries=USERBOT_CONNECTION_RETRIES" in source
    assert "retry_delay=USERBOT_RETRY_DELAY_SECONDS" in source
    assert "auto_reconnect=False" in source
