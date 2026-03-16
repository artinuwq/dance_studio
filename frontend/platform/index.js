export * from './telegram.js';
export * from './vk.js';
export * from './web.js';

export function getPlatform() {
  if (typeof window !== 'undefined' && window.Telegram?.WebApp) return 'telegram';
  if (typeof window !== 'undefined' && (window.vkBridge || /vk_platform/i.test(window.location.search))) return 'vk';
  return 'web';
}

export function isTelegram() { return getPlatform() === 'telegram'; }
export function isVK() { return getPlatform() === 'vk'; }
export function isPWA() { return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true; }
