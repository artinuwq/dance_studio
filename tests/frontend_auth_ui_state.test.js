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
