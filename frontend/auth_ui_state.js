(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
    return;
  }
  root.AuthUiState = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function normalizeBootstrapUser(bootstrapUser, profileData) {
    const authMethods = Array.isArray(bootstrapUser && bootstrapUser.auth_methods) ? bootstrapUser.auth_methods : [];
    const identities = (bootstrapUser && bootstrapUser.identities) || {};
    return {
      id: String((bootstrapUser && bootstrapUser.id) ?? (profileData && profileData.id) ?? ''),
      phone_verified: Boolean(bootstrapUser && bootstrapUser.phone_verified),
      requires_manual_merge: Boolean(bootstrapUser && bootstrapUser.requires_manual_merge),
      auth_methods: authMethods,
      identities: {
        telegram: { linked: Boolean(identities.telegram && identities.telegram.linked) },
        vk: { linked: Boolean(identities.vk && identities.vk.linked) },
        passkey: {
          linked: Boolean(identities.passkey && identities.passkey.linked),
          count: Number((identities.passkey && identities.passkey.count) || 0),
          items: Array.isArray(identities.passkey && identities.passkey.items) ? identities.passkey.items : [],
        },
        phone: {
          linked: Boolean(identities.phone && identities.phone.linked),
          verified: Boolean(identities.phone && identities.phone.verified),
        },
      },
      profile: profileData || null,
      deprecated: (bootstrapUser && bootstrapUser.deprecated) || {},
    };
  }

  function requiresVerifiedPhoneForBooking(authUser) {
    return !Boolean(authUser && authUser.phone_verified);
  }

  function getPhoneVerificationRequiredMessage() {
    return 'Сначала подтвердите номер телефона в профиле, затем отправляйте заявку.';
  }

  function shouldShowVerificationBanner(authUser) {
    return Boolean(authUser) && (!authUser.phone_verified || !authUser.identities.passkey.linked);
  }

  function getVerificationBannerText(state) {
    if (state && state.manualMergeRequired) {
      return 'Аккаунт требует ручной проверки перед объединением. Используйте другой способ входа или обратитесь в поддержку.';
    }
    if (state && state.verifiedPhoneConflict) {
      return 'По подтверждённому номеру найден конфликт. Выберите другой способ входа или напишите в поддержку.';
    }
    if (state && state.currentUser && !state.currentUser.phone_verified) {
      return 'Подтвердите номер телефона, чтобы оформлять записи и безопасно объединять способы входа.';
    }
    return 'Объединяйте входы через Telegram, VK и сайт, быстрее восстанавливайте доступ и включайте быстрый вход.';
  }

  function getIdentityRows(authUser) {
    const identities = (authUser && authUser.identities) || { telegram: {}, vk: {}, phone: {}, passkey: {} };
    return [
      ['Telegram', identities.telegram && identities.telegram.linked ? 'подключён' : 'не подключён'],
      ['VK', identities.vk && identities.vk.linked ? 'подключён' : 'не подключён'],
      ['Телефон', identities.phone && identities.phone.verified ? 'подтверждён' : 'не подтверждён'],
      ['Passkey', identities.passkey && identities.passkey.linked ? `настроен${identities.passkey.count ? ` (${identities.passkey.count})` : ''}` : 'не настроен'],
    ];
  }

  function getAuthStatusText(state) {
    if (state && state.manualMergeRequired) {
      return 'manual_merge_required — для продолжения используйте поддержку или альтернативный вход.';
    }
    if (state && state.verifiedPhoneConflict) {
      return 'verified_phone_conflict — попробуйте другой способ входа.';
    }
    if (state && Array.isArray(state.fallbackAuthMethods) && state.fallbackAuthMethods.length) {
      return `Доступные способы входа: ${state.fallbackAuthMethods.join(', ')}`;
    }
    if (state && state.currentUser && state.currentUser.id) {
      return `Текущий пользователь: #${state.currentUser.id}`;
    }
    return '';
  }

  function normalizeAuthResponse(payload, provider) {
    const authError = payload && payload.error ? payload.error : null;
    return {
      authProvider: provider || null,
      fallbackAuthMethods: Array.isArray(payload && payload.fallback_auth_methods) ? payload.fallback_auth_methods : [],
      manualMergeRequired: authError === 'manual_merge_required',
      verifiedPhoneConflict: authError === 'verified_phone_conflict',
      authError,
      action: payload && payload.action ? payload.action : null,
      message: payload && payload.message ? payload.message : null,
      linked: Boolean(payload && payload.linked),
    };
  }

  function shouldRefreshBootstrapAfterAuthAction(action) {
    return [
      'telegram_login',
      'vk_login',
      'phone_login',
      'telegram_link',
      'vk_link',
      'phone_link',
      'passkey_register',
      'passkey_delete',
      'merge_complete',
    ].includes(String(action || ''));
  }

  return {
    normalizeBootstrapUser,
    requiresVerifiedPhoneForBooking,
    getPhoneVerificationRequiredMessage,
    shouldShowVerificationBanner,
    getVerificationBannerText,
    getIdentityRows,
    getAuthStatusText,
    normalizeAuthResponse,
    shouldRefreshBootstrapAfterAuthAction,
  };
});
