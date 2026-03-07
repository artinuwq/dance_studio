from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NEWS_ROUTE = ROOT / "src" / "dance_studio" / "web" / "routes" / "news.py"
ADMIN_ROUTE = ROOT / "src" / "dance_studio" / "web" / "routes" / "admin.py"


def test_news_routes_sanitize_stored_text_fields():
    source = NEWS_ROUTE.read_text(encoding="utf-8")

    assert "title = sanitize_plain_text(data.get(\"title\"), multiline=False)" in source
    assert "content = sanitize_plain_text(data.get(\"content\"))" in source
    assert "\"title\": sanitize_plain_text(news.title, multiline=False) or \"\"" in source


def test_direction_routes_sanitize_text_fields():
    source = ADMIN_ROUTE.read_text(encoding="utf-8")

    assert "def _sanitize_direction_title(value):" in source
    assert "title = _sanitize_direction_title(data.get(\"title\"))" in source
    assert "description = _sanitize_direction_description(data.get(\"description\"))" in source
    assert "\"title\": _sanitize_direction_title(direction.title)" in source
