/* Public marketing only. No Google requests before explicit analytics consent. */
(() => {
  'use strict';
  const script = document.currentScript;
  const id = script && script.dataset.gaId;
  if (!/^G-[A-Z0-9]+$/.test(id || '')) return;
  if (navigator.globalPrivacyControl || navigator.doNotTrack === '1') return;
  const banner = document.getElementById('marketing-consent');
  const key = 'shvya-marketing-consent-v1';
  let active = false;
  let started = false;
  const memory = new Set();
  const read = (store, name) => { try { return window[store].getItem(name); } catch (_) { return null; } };
  const write = (store, name, value) => { try { window[store].setItem(name, value); } catch (_) { /* Storage may be blocked. */ } };
  window.dataLayer = window.dataLayer || [];
  const gtag = function () { window.dataLayer.push(arguments); };
  const event = (name, params = {}) => { if (active) gtag('event', name, params); };
  const once = (name, token, params) => {
    const marker = 'shvya-ga-' + name + ':' + token;
    if (memory.has(marker) || read('sessionStorage', marker)) return;
    if (!active) return;
    event(name, params);
    memory.add(marker);
    write('sessionStorage', marker, '1');
  };
  const conversions = () => {
    const saved = document.querySelector('[data-analytics-lead]');
    const signup = document.querySelector('[data-analytics-signup]');
    if (signup) once('sign_up', signup.dataset.analyticsSignup, {method: 'email'});
    if (saved) once('generate_lead', saved.dataset.analyticsLead, {method: 'demo_request'});
  };
  const enable = () => {
    active = true;
    window['ga-disable-' + id] = false;
    if (!started) {
      started = true;
      gtag('consent', 'default', {analytics_storage: 'granted', ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied'});
      gtag('js', new Date());
      // Exclude query strings, fragments, booking references and submitted form values.
      let referrer = '';
      try { const source = new URL(document.referrer);
        if (['www.google.com', 'google.com', 'www.google.co.in', 'www.bing.com', 'bing.com', 'duckduckgo.com'].includes(source.hostname)) referrer = source.origin;
      } catch (_) { /* Direct visit. */ }
      gtag('config', id, {send_page_view: false, allow_google_signals: false, allow_ad_personalization_signals: false,
        page_location: 'https://shvya-ai.com' + location.pathname, page_referrer: referrer,
        page_title: 'SHVYA AI public website', cookie_expires: 7776000, cookie_update: false});
      const tag = document.createElement('script');
      tag.async = true;
      tag.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(id);
      document.head.appendChild(tag);
      event('page_view');
      if (document.querySelector('.blog-article-heading')) event('article_view');
    } else {
      gtag('consent', 'update', {analytics_storage: 'granted'});
    }
    conversions();
  };
  const choose = value => {
    write('localStorage', key, value);
    if (banner) banner.hidden = true;
    if (value === 'granted') enable();
    else {
      active = false;
      window['ga-disable-' + id] = true;
      if (started) gtag('consent', 'update', {analytics_storage: 'denied'});
      // Remove this deployment's first-party GA cookies on withdrawal.
      const names = ['_ga', '_ga_' + id.slice(2)];
      names.forEach(name => ['', location.hostname, '.shvya-ai.com'].forEach(domain => {
        document.cookie = name + '=; Max-Age=0; Path=/; SameSite=Lax' + (domain ? '; Domain=' + domain : '');
      }));
    }
  };
  document.querySelectorAll('[data-analytics-consent]').forEach(button => {
    button.addEventListener('click', () => choose(button.dataset.analyticsConsent));
  });
  document.getElementById('marketing-consent-settings')?.addEventListener('click', () => { if (banner) banner.hidden = false; });
  if (read('localStorage', key) === 'granted') enable();
  else if (read('localStorage', key) !== 'denied' && banner) banner.hidden = false;
  window.addEventListener('storage', e => {
    if (e.key === key && e.newValue !== 'granted') choose('denied');
  });
  document.addEventListener('click', e => {
    const link = e.target.closest('a[href]');
    if (!link) return;
    let url;
    try { url = new URL(link.href, location.origin); } catch (_) { return; }
    if (url.pathname === '/book-a-call/' && url.origin === location.origin) event('demo_click');
    else if (['wa.me', 'api.whatsapp.com'].includes(url.hostname)) event('whatsapp_click');
    else if (url.protocol === 'tel:') event('phone_click');
    else if (url.protocol === 'mailto:') event('email_click');
    else if (url.origin !== location.origin && /^https?:$/.test(url.protocol)) event('outbound_click');
  });
  document.addEventListener('submit', e => {
    if (new URL(e.target.action, location.origin).pathname === '/book-a-call/') event('demo_form_submit');
  });
  const thresholds = new Set();
  window.addEventListener('scroll', () => {
    if (!active) return;
    const height = document.documentElement.scrollHeight - innerHeight;
    if (height <= 0) return;
    const percent = Math.round(scrollY / height * 100);
    [50, 90].forEach(value => {
      if (percent >= value && !thresholds.has(value)) {
        thresholds.add(value);
        event('content_scroll', {percent_scrolled: value});
      }
    });
  }, {passive: true});
})();
