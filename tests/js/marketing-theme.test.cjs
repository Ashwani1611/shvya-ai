'use strict';
// Dependency-free regression suite: node --test tests/js/marketing-theme.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/marketing/theme.js'), 'utf8');
const KEY = 'shvya.marketing.theme';

function boot({ dark = false, saved = null, blocked = false, legacy = false, noMedia = false, noSheet = false, loading = false } = {}) {
  const docEvents = {}, winEvents = {}, mediaEvents = {};
  const root = { dataset: {}, style: {} };
  const sheet = { media: '(prefers-color-scheme: light)' };
  const metas = [{ content: '#f5f5f7' }, { content: '#080909' }];
  const button = { attrs: {}, disabled: false, setAttribute(k, v) { this.attrs[k] = v; } };
  const writes = [];
  const storage = {
    getItem() { if (blocked) throw Error('Storage disabled'); return saved; },
    setItem(k, v) { if (blocked) throw Error('Storage disabled'); saved = v; writes.push([k, v]); },
  };
  let ready = !loading;
  const media = { matches: dark };
  if (legacy) media.addListener = fn => { mediaEvents.change = fn; };
  else media.addEventListener = (name, fn) => { mediaEvents[name] = fn; };
  const document = {
    documentElement: root, readyState: loading ? 'loading' : 'complete',
    getElementById: () => noSheet ? null : sheet,
    querySelectorAll(selector) { return selector.includes('theme-color') ? metas : ready ? [button] : []; },
    addEventListener(name, fn) { docEvents[name] = fn; },
  };
  const window = {
    localStorage: storage,
    addEventListener(name, fn) { winEvents[name] = fn; },
  };
  if (!noMedia) window.matchMedia = () => media;
  vm.runInNewContext(source, { document, window });
  return {
    root, sheet, metas, button, writes,
    click() { docEvents.click({ target: { closest: () => button } }); },
    unrelatedClick() { docEvents.click({ target: { closest: () => null } }); },
    system(value) { media.matches = value; mediaEvents.change?.({ matches: value }); },
    stored(value, key = KEY, area = storage) { saved = value; winEvents.storage({ key, storageArea: area }); },
    restore(value) { saved = value; winEvents.pageshow({ persisted: true }); },
    ready() { ready = true; docEvents.DOMContentLoaded(); },
  };
}

for (const dark of [false, true]) {
  test(`first visit follows ${dark ? 'dark' : 'light'} device without saving an override`, () => {
    const b = boot({ dark });
    assert.equal(b.root.dataset.marketingTheme, dark ? 'dark' : 'light');
    assert.equal(b.sheet.media, dark ? 'not all' : 'all');
    assert.equal(b.root.style.colorScheme, dark ? 'dark' : 'light');
    assert.equal(b.metas[0].content, dark ? '#080909' : '#f5f5f7');
    assert.deepEqual(b.writes, []);
  });
  test(`saved ${dark ? 'light' : 'dark'} overrides the opposite device preference`, () => {
    const saved = dark ? 'light' : 'dark';
    assert.equal(boot({ dark, saved }).root.dataset.marketingTheme, saved);
  });
}
test('header click and subsequent click persist valid opposite appearances', () => {
  const b = boot();
  b.click();
  assert.equal(b.root.dataset.marketingTheme, 'dark');
  assert.equal(b.button.attrs['aria-label'], 'Switch to day theme');
  b.click();
  assert.equal(b.root.dataset.marketingTheme, 'light');
  assert.equal(b.button.attrs.title, 'Switch to night theme');
  assert.deepEqual(b.writes, [[KEY, 'dark'], [KEY, 'light']]);
});
test('automatic mode responds to device changes in both directions', () => {
  const b = boot(); b.system(true);
  assert.equal(b.root.dataset.marketingTheme, 'dark');
  b.system(false);
  assert.equal(b.root.dataset.marketingTheme, 'light');
  assert.equal(b.writes.length, 0);
});
test('device changes never overwrite an explicit visitor choice', () => {
  const b = boot({ saved: 'dark' }); b.system(false);
  assert.equal(b.root.dataset.marketingTheme, 'dark');
  b.click(); b.system(true);
  assert.equal(b.root.dataset.marketingTheme, 'light');
});
test('blocked storage still allows a manual in-memory toggle', () => {
  const b = boot({ dark: true, blocked: true }); b.click(); b.system(true);
  assert.equal(b.root.dataset.marketingTheme, 'light');
  b.restore(null);
  assert.equal(b.root.dataset.marketingTheme, 'light');
});
test('invalid stored values follow the device instead of entering an invalid theme', () => {
  for (const saved of ['system', '', 'LIGHT', '<script>', 'null']) {
    assert.equal(boot({ saved, dark: true }).root.dataset.marketingTheme, 'dark');
  }
});
test('storage events sync tabs and removal restores live system-following mode', () => {
  const b = boot(); b.stored('dark');
  assert.equal(b.root.dataset.marketingTheme, 'dark');
  b.stored(null); assert.equal(b.root.dataset.marketingTheme, 'light');
  b.system(true); assert.equal(b.root.dataset.marketingTheme, 'dark');
});
test('storage.clear restores system appearance', () => {
  const b = boot({ saved: 'dark' }); b.stored(null, null);
  assert.equal(b.root.dataset.marketingTheme, 'light');
});
test('unrelated storage keys and other storage areas are ignored', () => {
  const b = boot(); b.stored('dark', 'dashboard-theme');
  assert.equal(b.root.dataset.marketingTheme, 'light');
  b.stored('dark', KEY, {});
  assert.equal(b.root.dataset.marketingTheme, 'light');
});
test('back/forward cache restore re-reads the current preference', () => {
  const b = boot({ saved: 'dark' }); b.restore('light');
  assert.equal(b.root.dataset.marketingTheme, 'light');
});
test('head initializes before DOM ready and labels update when header appears', () => {
  const b = boot({ loading: true, dark: true });
  assert.equal(b.root.dataset.marketingTheme, 'dark');
  assert.deepEqual(b.button.attrs, {});
  b.ready(); assert.equal(b.button.attrs['aria-label'], 'Switch to day theme');
});
test('older MediaQueryList listeners remain supported', () => {
  const b = boot({ legacy: true }); b.system(true);
  assert.equal(b.root.dataset.marketingTheme, 'dark');
});
test('browsers without matchMedia default to light and retain manual choice', () => {
  const b = boot({ noMedia: true }); assert.equal(b.root.dataset.marketingTheme, 'light');
  b.click(); assert.equal(b.root.dataset.marketingTheme, 'dark');
});
test('script is a no-op outside the public layout', () => {
  assert.deepEqual(boot({ noSheet: true }).root.dataset, {});
});
test('other clicks and disabled controls do not change appearance', () => {
  const b = boot(); b.unrelatedClick();
  b.button.disabled = true; b.click();
  assert.equal(b.root.dataset.marketingTheme, 'light');
  assert.equal(b.writes.length, 0);
});
