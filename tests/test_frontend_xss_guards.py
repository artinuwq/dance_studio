from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "index.html"


def test_frontend_uses_safe_text_helpers_for_news_and_directions():
    source = FRONTEND.read_text(encoding="utf-8")

    assert "function toSafeText(value, fallback = '')" in source
    assert "function toSafePreviewText(value, limit = 100)" in source
    assert "const NEWS_CACHE_KEY = 'news_cache_v2';" in source
    assert "const NEWS_ETAG_KEY = 'news_etag_v2';" in source
    assert "function clearLegacyNewsCache()" in source
    assert "localStorage.removeItem('news_cache_v1');" in source
    assert "localStorage.removeItem('news_etag_v1');" in source
    assert "clearLegacyNewsCache();" in source
    assert "const safeTitle = toSafeText(d.title" in source
    assert "const safeDescription = toSafeText(d.description" in source
    assert "const safePreview = toSafePreviewText(n.content, 100);" in source
    assert "const safeDirectionDescription = toSafeText(item.direction_description" in source
    assert "const safeTitle = toSafeText(n.title);" in source
