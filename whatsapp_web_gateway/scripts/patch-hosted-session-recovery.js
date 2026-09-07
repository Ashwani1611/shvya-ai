'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');

function replaceOnce(before, after, label) {
  const first = source.indexOf(before);
  if (first === -1) {
    if (source.includes(after)) {
      console.log(`Hosted session recovery patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted session recovery patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted session recovery signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted session recovery patch: ${label}`);
}

replaceOnce(
  `  client.initialize().catch(async (error) => {\n    state.status = 'failed';\n    state.qr = null;\n    state.qrGeneratedAt = 0;\n    state.lastError = error.message || String(error);\n    await callback(sessionId, 'failed', { error: state.lastError });\n  });`,
  `  client.initialize().catch(async (error) => {\n    state.status = 'failed';\n    state.qr = null;\n    state.qrGeneratedAt = 0;\n    state.lastError = error.message || String(error);\n    console.warn(\`Hosted session \${sessionId} failed to initialize:\`, state.lastError);\n    await callback(sessionId, 'failed', { error: state.lastError });\n    // A failed Chromium/browser startup must not keep a Redis lease alive.\n    // Keeping the failed state in memory lets the UI expose the error, while\n    // releasing the lease allows an explicit refresh or another healthy\n    // gateway instance to recover the persisted LocalAuth profile.\n    await releaseLock(sessionId).catch(() => {});\n  });`,
  'release lease after browser initialization failure',
);

replaceOnce(
  `    await startRedis();\n    await restoreSessions();\n    app.listen(PORT, '0.0.0.0', () => {`,
  `    await startRedis();\n    await restoreSessions();\n    // A previous gateway can leave a valid 90-second Redis lease behind when\n    // it is killed before graceful shutdown. Startup restoration used to try\n    // only once, so those persisted sessions stayed absent until another\n    // manual restart. Re-scan periodically; createSession is idempotent for\n    // sessions already owned by this process and will pick up skipped profiles\n    // as soon as their stale lease expires.\n    setInterval(() => {\n      restoreSessions().catch((error) => {\n        console.warn('Could not retry persisted Hosted sessions:', error.message);\n      });\n    }, 30000).unref();\n    app.listen(PORT, '0.0.0.0', () => {`,
  'retry persisted sessions after stale lease expiry',
);

fs.writeFileSync(target, source);

for (const marker of [
  'failed to initialize:',
  'releaseLock(sessionId).catch',
  'Could not retry persisted Hosted sessions:',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted session recovery patch verification failed: ${marker}`);
  }
}

console.log('Hosted session recovery runtime patch complete.');
