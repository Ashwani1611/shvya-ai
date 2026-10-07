const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../templates/channels/whatsapp_connect_hosted.html'), 'utf8');
const script = template.slice(template.indexOf('let activeQrUrl='), template.indexOf("document.getElementById('create-session-form').addEventListener"));
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function response(payload) { return { ok: true, json: async () => ({ ok: true, ...payload }) }; }
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const nodes = new Map(), requests = [], timeouts = new Map(), intervals = new Map();
  let timerId = 0, reloads = 0;
  function node(id) {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { textContent: '', disabled: false, src: '',
        classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
          toggle: (x, enabled) => enabled ? classes.add(x) : classes.delete(x) },
        removeAttribute(name) { this[name] = ''; } });
    }
    return nodes.get(id);
  }
  const context = vm.createContext({
    document: { getElementById: node }, window: { addEventListener() {} },
    AbortController, csrf: 'test', open() {},
    closeAll() { vm.runInContext('stopQr()', context); },
    setTimeout(fn, delay) { const id = ++timerId; timeouts.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
    setInterval(fn, delay) { const id = ++timerId; intervals.set(id, { fn, delay }); return id; },
    clearInterval(id) { intervals.delete(id); },
    location: { reload() { reloads++; } },
    fetch(url, options) { const wait = deferred(); requests.push({ url, options, ...wait }); return wait.promise; },
  });
  vm.runInContext(script, context);
  return { node, requests, intervals, timeouts, reloads: () => reloads,
    run: code => vm.runInContext(code, context),
    timer(delay) { const entry = [...timeouts].find(([, timer]) => timer.delay === delay); assert.ok(entry, `timer ${delay}`); timeouts.delete(entry[0]); return entry[1].fn(); },
  };
}

test('QR polling waits for the outstanding request before scheduling another', async () => {
  const h = harness(); h.run("showQr('a','/a/qr','/a/refresh')"); h.run('fetchQr()');
  assert.equal(h.requests.length, 1);
  assert.equal([...h.timeouts.values()].some(x => x.delay === 2000), false);
  h.requests[0].resolve(response({ status: 'qr_ready', qr: 'qr-a', expires_in: 30 })); await tick();
  h.timer(2000); assert.equal(h.requests.length, 2);
});

test('switching accounts aborts the old request and ignores its late QR response', async () => {
  const h = harness(); h.run("showQr('a','/a/qr','/a/refresh')");
  h.run("showQr('b','/b/qr','/b/refresh')");
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.requests[1].resolve(response({ status: 'qr_ready', qr: 'qr-b', expires_in: 30 })); await tick();
  h.requests[0].resolve(response({ status: 'qr_ready', qr: 'qr-a', expires_in: 30 })); await tick();
  assert.equal(h.node('qr-image').src, 'qr-b');
});

test('closing QR prevents late responses from painting or restarting polling', async () => {
  const h = harness(); h.run("showQr('a','/a/qr','/a/refresh')"); h.run('stopQr()');
  h.requests[0].resolve(response({ status: 'running' })); await tick();
  assert.equal(h.timeouts.size, 0); assert.equal(h.intervals.size, 0); assert.equal(h.reloads(), 0);
});

test('zero lifetime and an expired response never display a stale QR', async () => {
  const h = harness(); h.run("showQr('a','/a/qr','/a/refresh')");
  h.requests[0].resolve(response({ status: 'qr_ready', qr: 'old-qr', expires_in: 0 })); await tick();
  assert.equal(h.node('qr-image').src, '');
  h.timer(2000); h.requests[1].resolve(response({ status: 'expired' })); await tick();
  assert.equal(h.node('qr-image').classList.contains('hidden'), true);
  assert.match(h.node('qr-countdown').textContent, /expired/);
});

test('displayed QR is removed as soon as its countdown expires', async () => {
  const h = harness(); h.run("showQr('a','/a/qr','/a/refresh')");
  h.requests[0].resolve(response({ status: 'qr_ready', qr: 'qr-a', expires_in: 1 })); await tick();
  [...h.intervals.values()][0].fn();
  assert.equal(h.node('qr-image').src, ''); assert.equal(h.intervals.size, 0);
});

test('refresh is single-flight and late polls cannot overwrite its state', async () => {
  const h = harness(); h.run("showQr('a','/a/qr','/a/refresh')");
  h.run('refreshQr()'); h.run('refreshQr()');
  assert.equal(h.requests.length, 2); assert.equal(h.requests[1].options.method, 'POST');
  h.requests[0].resolve(response({ status: 'qr_ready', qr: 'old-qr', expires_in: 60 })); await tick();
  assert.equal(h.node('qr-image').src, ''); assert.equal(h.node('refresh-qr').disabled, true);
  h.requests[1].resolve(response({ status: 'initializing' })); await tick();
  assert.equal(h.node('refresh-qr').disabled, false); h.timer(1200);
  assert.equal(h.requests.length, 3);
});

test('closing a successfully linked QR cancels the pending reload', async () => {
  const h = harness(); h.run("showQr('a','/a/qr','/a/refresh')");
  h.requests[0].resolve(response({ status: 'qr_ready', qr: 'qr-a', expires_in: 60 })); await tick();
  h.timer(2000); h.requests[1].resolve(response({ status: 'running' })); await tick();
  assert.equal(h.node('qr-image').src, ''); assert.match(h.node('qr-status').textContent, /^Connected$/);
  h.run('stopQr()'); assert.equal(h.timeouts.size, 0); assert.equal(h.reloads(), 0);
});
