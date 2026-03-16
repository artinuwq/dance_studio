export async function initVkPlatform() {
  if (window.vkBridge?.send) {
    try { await window.vkBridge.send('VKWebAppInit'); } catch (_) {}
  }
}
