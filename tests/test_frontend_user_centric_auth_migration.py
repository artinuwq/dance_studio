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
    assert "vkPermissionRequired" in source
    assert "getVerificationBannerTitle" in helper_source
    assert "Способы входа" in source
    assert "Подтвердите аккаунт" in source
    assert "actionLabel: 'Подключить'" in source
    assert "Вход через Telegram Mini App" in source
    assert "Вход через VK Mini App" in source
    assert 'id="passkey-save-btn"' not in source
    assert 'id="passkey-delete-current-btn"' not in source
    assert 'id="passkey-delete-others-btn"' not in source
    assert 'id="auth-state-status"' not in source


def test_frontend_auth_headers_no_longer_use_telegram_id_as_primary_identity():
    source = _source()
    assert "headers['X-Telegram-Id']" not in source
    assert "telegram_user_id:" not in source
    assert "user_id: currentUserId" in source


def test_frontend_blocks_booking_flows_until_phone_is_verified():
    source = _source()
    helper_source = _helper_source()
    assert "function requiresVerifiedPhoneForBooking()" in source
    assert "function _hasVkNotificationPermissionIssue(channels)" in source
    assert "Сначала подтвердите номер телефона в профиле, затем отправляйте заявку." in helper_source
    assert "setRentalBookingMessage(getPhoneVerificationRequiredMessage(), true);" in source
    assert "setIndividualBookingMessage(getPhoneVerificationRequiredMessage(), true);" in source
    assert "showNotification(getPhoneVerificationRequiredMessage());" in source
    assert "window.AuthUiState.shouldRefreshBootstrapAfterAuthAction" in source


def test_frontend_vk_permission_flow_uses_server_permission_key():
    source = _source()
    assert "authFetch('/api/notifications/channels/vk/request-permission'" in source
    assert "permission_key: permissionContext.permissionKey" in source
    assert "groupId: Number(payload?.group_id || 0) > 0 ? Number(payload.group_id) : null," in source
    assert "requestVkAllowMessagesFromGroup(permissionContext.permissionKey, { groupId: permissionContext.groupId })" in source
    assert "VKWebAppAllowMessagesFromGroup', { group_id: groupId, key: normalizedPermissionKey }" in source


def test_frontend_vk_permission_flow_accepts_embedded_bridge_context():
    source = _source()
    assert "function _isVkEmbedded()" in source
    assert "window.vkBridge?.isEmbedded?.() === true" in source
    assert "const payload = await _sendVkBridgeWithTimeout('VKWebAppGetLaunchParams');" in source
    assert "const bridgeLaunchPayload = launchPayloadFromUrl ? null : await _readVkLaunchPayloadFromBridge();" in source
    assert "if ((!_isVkWebView() && !_isVkEmbedded()) || !window.vkBridge?.send)" in source
    assert "|| _isVkEmbedded();" in source


def test_frontend_vk_interactive_bridge_calls_no_longer_use_timeout_wrapper():
    source = _source()
    assert "async function _sendVkBridgeWithTimeout(method, params, timeoutMs = 1500)" in source
    assert "const result = await _sendVkBridge('VKWebAppAllowMessagesFromGroup', { group_id: groupId, key: normalizedPermissionKey });" in source


def test_frontend_telegram_phone_request_uses_live_webapp_instance():
    source = _source()
    assert "function getTelegramWebApp()" in source
    assert "await window.__tgSdkReady;" in source
    assert "const webApp = getTelegramWebApp();" in source
    assert "requestPhoneNumber" in source
    assert "requestPhone((result) => {" in source


def test_frontend_telegram_bootstrap_uses_shared_context_resolver():
    source = _source()
    assert "async function resolveTelegramContext(options = {})" in source
    assert "async function ensureTelegramAuthSession(options = {})" in source
    assert "return getTelegramContext().platform;" in source
    assert "const authSessionPromise = ensureAuthSession();" not in source
    assert "await ensureTelegramAuthSession();" in source


def test_profile_save_reuses_existing_profile_rendering_flow():
    source = _source()
    assert "function applyProfilePayloadToView(profilePayload)" in source
    assert "applyProfilePayloadToView(profilePayload);" in source
    assert "applyProfilePayloadToView(currentUserData);" in source
    assert "updateProfileHeader(currentUserData);" not in source


def test_staff_navigation_uses_bootstrap_snapshot_and_user_scoped_cache():
    source = _source()
    assert "function getCachedStaffStatusForUserId(userId)" in source
    assert "function applyBootstrapStaffStatus(bootstrapPayload)" in source
    assert "applyBootstrapStaffStatus(payload);" in source
    assert "storedAuthSnapshot?.currentUser?.id" in source
