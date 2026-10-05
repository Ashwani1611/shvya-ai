'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { patchSource } = require('../scripts/patch-hosted-realtime-delivery');

test('complete Docker source-patch chain assembles the realtime gateway', t => {
  const gateway = path.resolve(__dirname, '..');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-build-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.cpSync(path.join(gateway, 'src'), path.join(root, 'src'), { recursive: true });
  const dockerfile = fs.readFileSync(path.join(gateway, 'Dockerfile'), 'utf8');
  const scripts = [...dockerfile.matchAll(/^RUN node (scripts\/patch-hosted-[\w.-]+\.js)$/gm)].map(match => match[1]);
  assert.ok(scripts.includes('scripts/patch-hosted-realtime-delivery.js'));
  for (const script of scripts) execFileSync(process.execPath, [path.join(gateway, script)], { cwd: root });
  const source = fs.readFileSync(path.join(root, 'src/index.js'), 'utf8');
  execFileSync(process.execPath, ['--check', path.join(root, 'src/index.js')]);
  assert.equal(patchSource(source), source, 'final patch is idempotent');
  assert.equal((source.match(/sendSeen: false/g) || []).length, 3);
  assert.equal((source.match(/await ensureHostedSendReady\(req.params.sessionId, state\)/g) || []).length, 2);
  assert.match(source, /\['failed', 'disconnected'\]\.includes\(existing.status\)/);
  assert.match(source, /Hosted live chat lookup/);
  assert.match(source, /return callbackOutbox.enqueue/);
  assert.match(source, /shouldForwardMessageCreateFallback/);
  assert.match(source, /callbackOutbox: callbackOutbox\.stats\(\)/);
  assert.match(source, /return confirmSendOutcome/);
});

test('unknown gateway source fails closed instead of silently shipping a partial patch', () => {
  assert.throws(() => patchSource('unexpected gateway version'), /expected 1 matches/);
});
