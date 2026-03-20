from pathlib import Path


def _source() -> str:
    return Path('frontend/index.html').read_text(encoding='utf-8')


def test_frontend_has_user_centric_auth_state_and_bootstrap_contract():
    source = _source()
    assert "let currentAuthState = {" in source
    assert "currentUser: null" in source
    assert "getCurrentUserId()" in source
    assert "authFetch('/api/app/bootstrap')" in source
    assert "phone_verified" in source
    assert "requires_manual_merge" in source
    assert "fallback_auth_methods" in source


def test_frontend_renders_new_auth_states_and_identity_management_ui():
    source = _source()
    assert "manual_merge_required" in source
    assert "verified_phone_conflict" in source
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
