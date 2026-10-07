const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../templates/channels/whatsapp_connect_hosted.html'), 'utf8');
const statusScript = template.slice(template.indexOf("document.querySelectorAll('.js-status')"),
  template.indexOf("document.querySelectorAll('[data-health-card]')"));

async function poll(fetch) {
  const error = { textContent: '', hidden: true, classList: { toggle(name, value) { error.hidden = value; } } };
  const status = { textContent: 'Running', dataset: { statusUrl: '/status/' },
    closest() { return { querySelector() { return error; } }; } };
  vm.runInNewContext(statusScript, { document: { querySelectorAll() { return [status]; } }, fetch, setInterval() {} });
  await new Promise(resolve => setImmediate(resolve));
  return { status, error };
}

test('a rejected linked number replaces Running and displays its reason', async () => {
  const result = await poll(async () => ({ ok: true, json: async () => ({ ok: true, label: 'Failed',
    error: 'The linked WhatsApp number does not match this account.' }) }));
  assert.equal(result.status.textContent, 'Failed');
  assert.match(result.error.textContent, /does not match/);
  assert.equal(result.error.hidden, false);
});

test('HTTP gateway failure replaces the stale Running label', async () => {
  const result = await poll(async () => ({ ok: false, json: async () => ({ ok: false,
    label: 'Gateway unavailable', error: 'The Hosted connection could not be checked.' }) }));
  assert.equal(result.status.textContent, 'Gateway unavailable');
  assert.equal(result.error.hidden, false);
});

test('a network failure shows a safe connection error and recovery clears it', async () => {
  const failed = await poll(async () => { throw new Error('Private internal address'); });
  assert.equal(failed.status.textContent, 'Gateway unavailable');
  assert.doesNotMatch(failed.error.textContent, /Private internal/);
  const recovered = await poll(async () => ({ ok: true, json: async () => ({ ok: true, label: 'Running', error: '' }) }));
  assert.equal(recovered.status.textContent, 'Running');
  assert.equal(recovered.error.hidden, true);
});

test('slow status checks cannot overlap and repaint a newer state out of order', async () => {
  let finish, update, calls = 0;
  const error = { textContent: '', classList: { toggle() {} } };
  const status = { textContent: 'Connecting', dataset: { statusUrl: '/status/' },
    closest() { return { querySelector() { return error; } }; } };
  vm.runInNewContext(statusScript, {
    document: { querySelectorAll() { return [status]; } },
    fetch() { calls++; return new Promise(resolve => { finish = resolve; }); },
    setInterval(callback) { update = callback; },
  });
  await update(); await update(); assert.equal(calls, 1);
  finish({ ok: true, json: async () => ({ ok: true, label: 'Running' }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(status.textContent, 'Running');
  update(); assert.equal(calls, 2);
});
