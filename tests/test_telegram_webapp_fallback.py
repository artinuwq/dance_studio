from pathlib import Path


def test_fallback_loader_still_fetches_real_telegram_sdk_when_stub_has_init_data():
    source = Path("frontend/vendor/telegram-web-app.js").read_text(encoding="utf-8")

    assert "function hasRemoteSdkFeatures(webApp)" in source
    assert "function hasPhoneRequestApi(webApp)" in source
    assert "if (!hasRemoteSdkFeatures(window.Telegram?.WebApp)) {" in source
    assert "await loadRemoteSdk();" in source
    assert '"/assets/vendor/telegram-web-app-sdk.js?v=20260401"' in source
    assert "https://telegram.org/js/telegram-web-app.js" not in source


def test_frontend_uses_versioned_local_telegram_loader_and_bundled_sdk_exists():
    html_source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert '<script src="/assets/vendor/telegram-web-app.js?v=20260401"></script>' in html_source
    assert Path("frontend/vendor/telegram-web-app-sdk.js").exists()
