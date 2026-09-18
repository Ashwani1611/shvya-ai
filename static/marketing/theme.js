/* Public website only. Keep this blocking head script before the body to avoid
 * painting the wrong saved appearance. No dashboard settings or cookies used. */
(() => {
  'use strict';
  const root = document.documentElement;
  const daySheet = document.getElementById('marketing-day-theme');
  if (!daySheet) return;

  const key = 'shvya.marketing.theme';
  const valid = value => value === 'light' || value === 'dark';
  const system = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  // undefined means storage is blocked; preserve an in-memory choice in that case.
  function readPreference() {
    try {
      const value = window.localStorage.getItem(key);
      return valid(value) ? value : null;
    } catch (_) {
      return undefined;
    }
  }
  let preference = readPreference() || null;

  function render() {
    const theme = preference || (system && system.matches ? 'dark' : 'light');
    const light = theme === 'light';
    daySheet.media = light ? 'all' : 'not all';
    root.dataset.marketingTheme = theme;
    root.style.colorScheme = theme;
    root.style.backgroundColor = light ? '#f5f5f7' : '#080909';
    document.querySelectorAll('[data-marketing-theme-color]').forEach(meta => {
      meta.content = light ? '#f5f5f7' : '#080909';
    });
    document.querySelectorAll('[data-marketing-theme-toggle]').forEach(button => {
      const label = light ? 'Switch to night theme' : 'Switch to day theme';
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
    });
  }

  // Initial appearance is applied before body parsing, not after DOMContentLoaded.
  render();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render, { once: true });
  }
  document.addEventListener('click', event => {
    const target = event.target;
    const button = target && typeof target.closest === 'function'
      ? target.closest('[data-marketing-theme-toggle]') : null;
    if (!button || button.disabled) return;
    preference = root.dataset.marketingTheme === 'light' ? 'dark' : 'light';
    try { window.localStorage.setItem(key, preference); } catch (_) { /* Session-only choice. */ }
    render();
  });

  function followSystem() { if (!preference) render(); }
  if (system) {
    if (typeof system.addEventListener === 'function') {
      system.addEventListener('change', followSystem);
    } else if (typeof system.addListener === 'function') {
      system.addListener(followSystem);
    }
  }
  // Keep open tabs and back/forward-cache restores consistent with the saved choice.
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    try {
      if (event.storageArea && event.storageArea !== window.localStorage) return;
    } catch (_) { return; }
    const saved = readPreference();
    if (saved !== undefined) preference = saved;
    render();
  });
  window.addEventListener('pageshow', () => {
    const saved = readPreference();
    if (saved !== undefined) preference = saved;
    render();
  });
})();
