from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROUTES = ROOT / "src" / "dance_studio" / "web" / "routes" / "admin.py"
FRONTEND = ROOT / "frontend" / "index.html"


def _window(source: str, marker: str, size: int = 2200) -> str:
    index = source.find(marker)
    assert index != -1, f"Marker not found: {marker}"
    return source[index : index + size]


def test_studio_stats_route_exists_and_requires_permission():
    source = ADMIN_ROUTES.read_text(encoding="utf-8")
    marker = '@bp.route("/api/stats/studio", methods=["GET"])'
    window = _window(source, marker)

    assert "def get_studio_stats():" in window
    assert 'require_permission("view_stats")' in window
    assert '"expected_revenue_rub"' in window
    assert '"new_clients"' in window
    assert '"booking_requests_created"' in window


def test_frontend_stats_page_supports_studio_summary():
    source = FRONTEND.read_text(encoding="utf-8")

    assert 'id="studio-stats-date-from"' in source
    assert 'id="studio-stats-date-to"' in source
    assert 'id="studio-stats-result"' in source
    assert "async function loadStudioStats()" in source
    assert "fetch(`/api/stats/studio?" in source
