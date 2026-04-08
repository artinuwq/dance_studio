from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "src" / "dance_studio" / "db" / "models.py"
VERSIONS_DIR = ROOT / "alembic" / "versions"
MIGRATION = VERSIONS_DIR / "20260405_0001_baseline.py"
VK_ATTENDANCE_MIGRATION = VERSIONS_DIR / "20260406_0002_vk_att_msg_ids.py"


def test_group_chat_fields_removed_from_model():
    source = MODELS.read_text(encoding="utf-8")
    marker = "class Group(Base):"
    start = source.find(marker)
    assert start != -1
    group_window = source[start : start + 1200]

    assert "chat_id = Column(" not in group_window
    assert "chat_invite_link = Column(" not in group_window


def test_baseline_and_followup_migrations_exist():
    source = MIGRATION.read_text(encoding="utf-8")
    followup_source = VK_ATTENDANCE_MIGRATION.read_text(encoding="utf-8")

    version_files = sorted(path.name for path in VERSIONS_DIR.glob("*.py"))
    assert version_files == [
        "20260405_0001_baseline.py",
        "20260406_0002_vk_att_msg_ids.py",
    ]
    assert 'revision = "20260405_0001_baseline"' in source
    assert "down_revision = None" in source
    assert "Base.metadata.create_all" in source
    assert "Base.metadata.drop_all" in source
    assert 'revision = "20260406_0002_vk_att_msg_ids"' in followup_source
    assert 'down_revision = "20260405_0001_baseline"' in followup_source
