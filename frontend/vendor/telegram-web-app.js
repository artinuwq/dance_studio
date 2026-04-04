(() => {
  const LOCAL_SDK_VERSION = "dance-studio-fallback-2";
  const REMOTE_SDK_URL = "/assets/vendor/telegram-web-app-sdk.js?v=20260401";
  const status = {
    localVersion: LOCAL_SDK_VERSION,
    localScriptLoaded: true,
    remoteScriptTried: false,
    remoteScriptLoaded: false,
    usedHashFallback: false,
    hashHasWebAppData: false,
    hasTelegramObject: false,
    hasWebAppObject: false,
    hasInitData: false,
    hasSdkFeatures: false,
    hasPhoneRequestApi: false,
    startedAt: Date.now(),
  };

  window.__tgSdkDiagnostics = status;

  function parseLaunchParams() {
    const hashRaw = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : window.location.hash;
    const hashParams = new URLSearchParams(hashRaw);
    if (hashParams.size > 0) return hashParams;

    const searchRaw = window.location.search.startsWith("?") ? window.location.search.slice(1) : window.location.search;
    return new URLSearchParams(searchRaw);
  }

  function parseUnsafeData(initData) {
    const params = new URLSearchParams(initData || "");
    const rawUser = params.get("user");
    let parsedUser = null;
    if (rawUser) {
      try {
        parsedUser = JSON.parse(rawUser);
      } catch (_) {
        parsedUser = null;
      }
    }
    return {
      user: parsedUser,
      auth_date: params.get("auth_date"),
      query_id: params.get("query_id"),
      start_param: params.get("start_param"),
    };
  }

  function ensureNoopBackButton(backButton) {
    const target = backButton || {};
    if (typeof target.show !== "function") target.show = () => {};
    if (typeof target.hide !== "function") target.hide = () => {};
    if (typeof target.onClick !== "function") target.onClick = () => {};
    return target;
  }

  function ensureWebAppFromHash() {
    const launchParams = parseLaunchParams();
    const hashInitData = (launchParams.get("tgWebAppData") || "").trim();
    status.hashHasWebAppData = Boolean(hashInitData);

    const telegram = (window.Telegram = window.Telegram || {});
    const webApp = (telegram.WebApp = telegram.WebApp || {});
    const existingInitData = typeof webApp.initData === "string" ? webApp.initData.trim() : "";

    if (!existingInitData && hashInitData) {
      webApp.initData = hashInitData;
      webApp.initDataUnsafe = parseUnsafeData(hashInitData);
      status.usedHashFallback = true;
    }

    if (typeof webApp.ready !== "function") webApp.ready = () => {};
    if (typeof webApp.expand !== "function") webApp.expand = () => {};
    webApp.BackButton = ensureNoopBackButton(webApp.BackButton);

    status.hasTelegramObject = Boolean(window.Telegram);
    status.hasWebAppObject = Boolean(window.Telegram?.WebApp);
    status.hasInitData = Boolean(window.Telegram?.WebApp?.initData);
    status.hasSdkFeatures = hasRemoteSdkFeatures(window.Telegram?.WebApp);
    status.hasPhoneRequestApi = hasPhoneRequestApi(window.Telegram?.WebApp);
  }

  function hasPhoneRequestApi(webApp) {
    return Boolean(
      webApp
      && (
        typeof webApp.requestContact === "function"
        || typeof webApp.requestPhoneNumber === "function"
      )
    );
  }

  function hasRemoteSdkFeatures(webApp) {
    return Boolean(
      webApp
      && (
        typeof webApp.onEvent === "function"
        || typeof webApp.offEvent === "function"
        || typeof webApp.sendData === "function"
        || typeof webApp.close === "function"
        || hasPhoneRequestApi(webApp)
      )
    );
  }

  function loadRemoteSdk() {
    return new Promise((resolve) => {
      status.remoteScriptTried = true;
      const script = document.createElement("script");
      script.src = REMOTE_SDK_URL;
      script.async = true;
      script.onload = () => {
        status.remoteScriptLoaded = Boolean(window.Telegram?.WebApp);
        ensureWebAppFromHash();
        resolve(window.Telegram?.WebApp || null);
      };
      script.onerror = () => {
        status.remoteScriptLoaded = false;
        ensureWebAppFromHash();
        resolve(window.Telegram?.WebApp || null);
      };
      document.head.appendChild(script);
    });
  }

  function finalize() {
    ensureWebAppFromHash();
    status.readyAt = Date.now();
    return window.Telegram?.WebApp || null;
  }

  window.__tgSdkReady = (async () => {
    finalize();
    if (!hasRemoteSdkFeatures(window.Telegram?.WebApp)) {
      await loadRemoteSdk();
    }
    return finalize();
  })();
})();
