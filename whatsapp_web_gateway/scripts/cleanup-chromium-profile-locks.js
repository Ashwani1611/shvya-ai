'use strict';

const fs = require('fs');
const path = require('path');

const AUTH_PATH = process.env.WHATSAPP_WEB_SESSION_PATH || '/data/wwebjs_auth';
const LOCK_NAMES = ['SingletonCookie', 'SingletonLock', 'SingletonSocket'];

async function cleanupChromiumProfileLocks() {
  let entries;
  try {
    entries = await fs.promises.readdir(AUTH_PATH, { withFileTypes: true });
  } catch (error) {
    if (error && error.code === 'ENOENT') return;
    throw error;
  }

  let removed = 0;
  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith('session-')) continue;

    const profilePath = path.join(AUTH_PATH, entry.name);
    for (const lockName of LOCK_NAMES) {
      const lockPath = path.join(profilePath, lockName);
      try {
        await fs.promises.unlink(lockPath);
        removed += 1;
      } catch (error) {
        if (!error || error.code !== 'ENOENT') throw error;
      }
    }
  }

  if (removed) {
    console.log(`Removed ${removed} stale Chromium profile lock file(s).`);
  }
}

cleanupChromiumProfileLocks().catch((error) => {
  console.error('Failed to clean stale Chromium profile locks:', error);
  process.exit(1);
});
