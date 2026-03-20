from pathlib import Path


def _source() -> str:
    return Path('frontend/index.html').read_text(encoding='utf-8')


def _helper_source() -> str:
    return Path('frontend/auth_ui_state.js').read_text(encoding='utf-8')


def test_frontend_has_user_centric_auth_state_and_bootstrap_contract():
    source = _source()
    helper_source = _helper_source()
    assert "let currentAuthState = {" in source
    assert "currentUser: null" in source
    assert "getCurrentUserId()" in source
    assert "authFetch('/api/app/bootstrap')" in source
    assert "phone_verified" in helper_source
    assert "requires_manual_merge" in helper_source
    assert "fallback_auth_methods" in source
    assert "window.AuthUiState.normalizeBootstrapUser" in source


def test_frontend_renders_new_auth_states_and_identity_management_ui():
    source = _source()
    helper_source = _helper_source()
    assert "manual_merge_required" in helper_source
    assert "verified_phone_conflict" in helper_source
    assert "Способы входа" in source
    assert "Подтвердите аккаунт" in source
    assert "Подключить Telegram" in source
    assert "Подключить VK" in source
    assert "Удалить passkey" in source


def test_frontend_auth_headers_no_longer_use_telegram_id_as_primary_identity():
    source = _source()
    assert "headers['X-Telegram-Id']" not in source
    assert "telegram_user_id:" not in source
    assert "user_id: currentUserId" in source


def test_frontend_blocks_booking_flows_until_phone_is_verified():
    source = _source()
    helper_source = _helper_source()
    assert "function requiresVerifiedPhoneForBooking()" in source
    assert "Сначала подтвердите номер телефона в профиле, затем отправляйте заявку." in helper_source
    assert "setRentalBookingMessage(getPhoneVerificationRequiredMessage(), true);" in source
    assert "setIndividualBookingMessage(getPhoneVerificationRequiredMessage(), true);" in source
    assert "showNotification(getPhoneVerificationRequiredMessage());" in source
    assert "window.AuthUiState.shouldRefreshBootstrapAfterAuthAction" in source
