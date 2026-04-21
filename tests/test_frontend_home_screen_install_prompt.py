from pathlib import Path


def test_frontend_supports_home_screen_install_prompt_after_onboarding():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert 'data-home-screen-install-card' in source
    assert 'async function refreshHomeScreenInstallState(options = {})' in source
    assert 'async function requestAddToHomeScreen()' in source
    assert 'function dismissHomeScreenInstallPrompt()' in source
    assert "window.addEventListener('dance-home-screen-install-state-changed'" in source
    assert 'window.addEventListener(\'beforeinstallprompt\'' in source
    assert 'window.addEventListener(\'appinstalled\'' in source
    assert 'async function getHomeScreenInstallState()' in source
    assert 'async function addToHomeScreen()' in source
    assert 'getHomeScreenInstallState,' in source
    assert 'addToHomeScreen,' in source
    assert "await refreshHomeScreenInstallState({ force: true, resetDismiss: true });" in source


def test_frontend_uses_vk_bridge_for_home_screen_install_when_available():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "homeScreenInstallState.source === 'vk'" in source
    assert "async function _vkBridgeSupportsMethod(method)" in source
    assert "'VKWebAppAddToHomeScreenInfo'" in source
    assert "'VKWebAppAddToHomeScreen'" in source
    assert "const info = await _sendVkBridgeWithTimeout('VKWebAppAddToHomeScreenInfo'" in source
    assert "await _sendVkBridgeWithTimeout('VKWebAppAddToHomeScreen'" in source
    assert "throw new Error('vk_home_screen_unsupported');" in source


def test_frontend_home_screen_install_depends_only_on_account_and_phone():
    source = Path("frontend/index.html").read_text(encoding="utf-8")

    assert "const phoneVerified = window.AuthUiState.isPhoneVerified(authUser);" in source
    assert "return hasAccount && phoneVerified;" in source
