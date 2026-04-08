from pathlib import Path


def test_telegram_back_button_uses_live_webapp_instance():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "function getTelegramBackButton()" in source
    assert "function hasTelegramBackButton()" in source
    assert "let telegramBackButtonBoundTo = null;" in source
    assert "bindTelegramBackButton();" in source
    assert "window.__tgSdkReady && typeof window.__tgSdkReady.then === 'function'" in source
    assert "_setClientDetailBackFallbackVisible(!hasTelegramBackButton());" in source
    assert "_setAdminBookingRequestBackFallbackVisible(!hasTelegramBackButton());" in source
    assert "const hasTelegramBackButton = Boolean(tg && typeof tg.BackButton !== 'undefined');" not in source
