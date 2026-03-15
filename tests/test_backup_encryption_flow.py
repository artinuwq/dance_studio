from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_FILE = ROOT / "src" / "dance_studio" / "bot" / "bot.py"
SETTINGS_FILE = ROOT / "src" / "dance_studio" / "core" / "settings.py"


def _window(source: str, marker: str, size: int = 1600) -> str:
    index = source.find(marker)
    assert index != -1, f"Marker not found: {marker}"
    return source[index : index + size]


def test_backup_flow_prepares_artifacts_before_sending():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def create_and_send_backup(reason: str, notify_user_id: int | None = None) -> None:")

    assert "_prepare_backup_artifacts_for_send" in window
    assert "db_artifact_path" in window
    assert "media_artifact_path" in window


def test_backup_sends_encrypted_artifacts_variables():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def create_and_send_backup(reason: str, notify_user_id: int | None = None) -> None:", size=3200)

    assert "FSInputFile(str(db_artifact_path))" in window
    assert "FSInputFile(str(media_artifact_path))" in window


def test_backup_flow_uses_dedicated_delivery_bot():
    source = BOT_FILE.read_text(encoding="utf-8")
    window = _window(source, "async def create_and_send_backup(reason: str, notify_user_id: int | None = None) -> None:", size=3600)

    assert "async with _backup_delivery_bot() as backup_bot" in window
    assert "await _ensure_backup_topic_with_bot(backup_bot)" in window
    assert "await backup_bot.send_media_group(" in window


def test_backup_proxy_setting_is_defined():
    source = SETTINGS_FILE.read_text(encoding="utf-8")

    assert "BACKUP_TELEGRAM_PROXY" in source
