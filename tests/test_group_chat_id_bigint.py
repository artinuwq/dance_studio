from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "src" / "dance_studio" / "db" / "models.py"
MIGRATION = ROOT / "alembic" / "versions" / "20260307_0018_group_chat_bigint.py"


def test_group_chat_id_uses_bigint_in_model():
    source = MODELS.read_text(encoding="utf-8")

    assert "chat_id = Column(BigInteger, nullable=True)" in source


def test_group_chat_id_bigint_migration_exists():
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "20260307_0018_group_chat_bigint"' in source
    assert '"groups"' in source
    assert '"chat_id"' in source
    assert "type_=sa.BigInteger()" in source
