from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "admin.py"


def _window(source: str, marker: str, size: int = 2600) -> str:
    index = source.find(marker)
    assert index != -1, f"Marker not found: {marker}"
    return source[index : index + size]


def test_update_staff_requires_manage_staff_permission_to_change_position():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")
    window = _window(source, "def update_staff(staff_id):")

    assert 'if "position" in data:' in window
    assert 'position_perm_error = require_permission("manage_staff")' in window
    assert "if position_perm_error:" in window
