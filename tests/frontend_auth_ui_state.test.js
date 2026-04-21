const test = require('node:test');
const assert = require('node:assert/strict');
const state = require('../frontend/auth_ui_state.js');

test('shows verification banner only when phone or passkey is missing', () => {
  const unverified = state.normalizeBootstrapUser({ id: 1, phone_verified: false, identities: { telegram: {}, vk: {}, phone: { verified: false }, passkey: { linked: false, count: 0 } } });
  const verified = state.normalizeBootstrapUser({ id: 1, phone_verified: true, identities: { telegram: { linked: true }, vk: {}, phone: { verified: true }, passkey: { linked: true, count: 1 } } });
  assert.equal(state.shouldShowVerificationBanner(unverified), true);
  assert.equal(state.shouldShowVerificationBanner(verified), false);
});

test('booking gating points users to verify-phone flow', () => {
  const authUser = state.normalizeBootstrapUser({ id: 2, phone_verified: false, identities: { phone: { verified: false }, passkey: { linked: false, count: 0 } } });
  assert.equal(state.requiresVerifiedPhoneForBooking(authUser), true);
  assert.equal(state.getPhoneVerificationRequiredMessage(), 'Сначала подтвердите номер телефона в профиле, затем отправляйте заявку.');
});

test('identity rows and status text reflect linked providers and merge/conflict states', () => {
  const authUser = state.normalizeBootstrapUser({
    id: 3,
    phone_verified: true,
    identities: {
      telegram: { linked: true },
      vk: { linked: true },
      phone: { linked: true, verified: true },
      passkey: { linked: true, count: 2, items: [{ credential_id: 'cred-1' }, { credential_id: 'cred-2' }] },
    },
  });
  const rows = state.getIdentityRows(authUser);
  assert.deepEqual(rows, [
    ['Telegram', 'подключён'],
    ['VK', 'подключён'],
    ['Телефон', 'подтверждён'],
    ['Passkey', 'настроен (2)'],
  ]);
  assert.equal(state.getAuthStatusText({ currentUser: authUser, fallbackAuthMethods: [] }), 'Текущий пользователь: #3');
  assert.match(state.getVerificationBannerText({ currentUser: authUser, manualMergeRequired: true }), /ручной проверки/);
  assert.match(state.getVerificationBannerText({ currentUser: authUser, verifiedPhoneConflict: true }), /конфликт/);
});

test('vk permission banner explains that community messages must be allowed again', () => {
  const authUser = state.normalizeBootstrapUser({
    id: 4,
    phone_verified: true,
    identities: {
      vk: { linked: true },
      phone: { linked: true, verified: true },
      passkey: { linked: false, count: 0, items: [] },
    },
  });
  const bannerState = { currentUser: authUser, vkPermissionRequired: true };
  assert.equal(state.getVerificationBannerTitle(bannerState), 'Включите сообщения VK');
  assert.match(state.getVerificationBannerText(bannerState), /VK Mini App/);
  assert.match(state.getVerificationBannerText(bannerState), /сообщения от сообщества/);
});

test('verification banner actions switch from phone confirmation to vk permission step', () => {
  const phonePendingUser = state.normalizeBootstrapUser({
    id: 5,
    phone_verified: false,
    identities: {
      phone: { linked: false, verified: false },
      vk: { linked: true },
    },
  });
  assert.deepEqual(
    state.getVerificationBannerActions({ currentUser: phonePendingUser, vkPermissionRequired: true }),
    {
      primaryLabel: 'Подтвердить номер',
      primaryAction: 'verify_phone',
      secondaryLabel: '',
      secondaryAction: null,
    },
  );

  const vkPendingUser = state.normalizeBootstrapUser({
    id: 6,
    phone_verified: true,
    identities: {
      phone: { linked: true, verified: true },
      vk: { linked: true },
    },
  });
  assert.deepEqual(
    state.getVerificationBannerActions({ currentUser: vkPendingUser, vkPermissionRequired: true }),
    {
      primaryLabel: 'Разрешить писать сообществу',
      primaryAction: 'verify_vk_messages',
      secondaryLabel: 'Позже',
      secondaryAction: 'skip_vk_messages',
    },
  );
});

test('normalizes auth responses and refresh policy for link/login/passkey actions', () => {
  assert.deepEqual(
    state.normalizeAuthResponse({ error: 'verified_phone_conflict', fallback_auth_methods: ['telegram'], action: 'contact_support', message: 'Conflict' }, 'vk'),
    {
      authProvider: 'vk',
      fallbackAuthMethods: ['telegram'],
      manualMergeRequired: false,
      verifiedPhoneConflict: true,
      authError: 'verified_phone_conflict',
      action: 'contact_support',
      message: 'Conflict',
      linked: false,
    },
  );
  assert.equal(state.shouldRefreshBootstrapAfterAuthAction('telegram_link'), true);
  assert.equal(state.shouldRefreshBootstrapAfterAuthAction('passkey_delete'), true);
  assert.equal(state.shouldRefreshBootstrapAfterAuthAction('unknown_action'), false);
});

test('detects guest/authenticated state by current user id', () => {
  assert.equal(state.hasCurrentUser({ currentUser: { id: 42 } }), true);
  assert.equal(state.hasCurrentUser({ currentUser: { id: '  ' } }), false);
  assert.equal(state.hasCurrentUser({ currentUser: null }), false);
  assert.equal(state.hasCurrentUser({}), false);
});
