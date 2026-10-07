/* A light enhancement layer; search and article navigation still work without JS. */
(() => {
  'use strict';

  const app = document.querySelector('.kb-app');
  if (!app) return;

  const toggle = app.querySelector('[data-kb-nav-toggle]');
  const close = app.querySelector('[data-kb-nav-close]');
  const sidebar = app.querySelector('#kb-sidebar');
  const search = app.querySelector('#kb-search-input');

  function setNav(open) {
    app.classList.toggle('kb-nav-open', Boolean(open));
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && sidebar) sidebar.focus?.({ preventScroll: true });
  }

  toggle?.addEventListener('click', () => setNav(!app.classList.contains('kb-nav-open')));
  close?.addEventListener('click', () => setNav(false));
  sidebar?.querySelectorAll('a').forEach(anchor => {
    anchor.addEventListener('click', () => setNav(false));
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      if (app.classList.contains('kb-nav-open')) {
        setNav(false);
        toggle?.focus();
      }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      search?.focus();
      search?.select();
    }
  });

  const copy = app.querySelector('[data-kb-copy-link]');
  copy?.addEventListener('click', async () => {
    if (!navigator.clipboard?.writeText) {
      copy.textContent = 'Copy link from address bar';
      return;
    }
    try {
      await navigator.clipboard.writeText(location.href.split('#')[0]);
      copy.textContent = 'Link copied ✓';
      window.setTimeout(() => { copy.textContent = 'Copy article link ↗'; }, 2500);
    } catch (_) {
      copy.textContent = 'Copy link from address bar';
    }
  });

  const anchors = Array.from(app.querySelectorAll('.kb-on-this-page nav a[href^="#"]'));
  if (anchors.length && 'IntersectionObserver' in window) {
    const sections = anchors.map(a => document.getElementById(a.hash.slice(1))).filter(Boolean);
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          anchors.forEach(a => a.classList.toggle('is-visible', a.hash === '#' + entry.target.id));
        }
      }
    }, { rootMargin: '-8% 0px -78% 0px', threshold: 0 });
    sections.forEach(section => observer.observe(section));
  }
})();