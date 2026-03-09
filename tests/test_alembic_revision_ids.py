import re
from pathlib import Path


def test_alembic_revision_ids_fit_default_version_table_limit():
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    revision_re = re.compile(r'^revision\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)

    too_long: list[str] = []
    for path in sorted(versions_dir.glob("*.py")):
        content = path.read_text(encoding="utf-8-sig")
        match = revision_re.search(content)
        if not match:
            continue
        revision = match.group(1)
        if len(revision) > 32:
            too_long.append(f"{path.name}: {len(revision)} ({revision})")

    assert not too_long, (
        "Alembic revision IDs must be <= 32 chars for default alembic_version.version_num:\n"
        + "\n".join(too_long)
    )
