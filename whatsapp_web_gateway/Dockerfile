FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ffmpeg ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package*.json ./
RUN npm ci --omit=dev

# whatsapp-web.js 1.34.7 is currently incompatible with WhatsApp Web's
# 2.3000.104xxx LID/message-key changes. Apply the reviewed upstream fix
# during the image build until it is released by whatsapp-web.js.
COPY scripts ./scripts
RUN node scripts/patch-whatsapp-web-1.34.7.js

# Copy the gateway source before applying SHVYA's runtime source patch.
# patch-hosted-chat-complete-sync.js reads and updates src/index.js.
COPY src ./src
# Harden SHVYA's gateway for current LID identities, larger searchable chat
# indexes, resilient callbacks, and linked-device realtime message delivery.
RUN node scripts/patch-hosted-chat-complete-sync.js
# Recover persisted Hosted sessions automatically after stale Redis leases
# expire, and release leases when Chromium initialization fails.
RUN node scripts/patch-hosted-session-recovery.js
# Every active in-memory session must continuously own a Redis lease. Recreate
# missing leases safely, and fence the local client if a foreign owner appears.
RUN node scripts/patch-hosted-lease-invariant.js
# If a persisted profile itself fails Chromium initialization, retry that
# failed in-memory state safely instead of treating it as already restored.
RUN node scripts/patch-hosted-failed-session-retry.js
# Clean stale Chromium SingletonLock/Cookie/Socket files before every retry
# and cap runaway restart storms with exponential backoff (root cause of the
# Sept 2026 zombie-Chromium incident: 8,520 procs from unbounded 30s retries).
RUN node scripts/patch-hosted-chromium-lock-cleanup.js
# Expire abandoned QR sessions, clean their unpaired LocalAuth profile, release
# their lease, and expose per-state counts from the gateway health endpoint.
RUN node scripts/patch-hosted-qr-expiry.js
# Preserve the original whatsapp-web.js message type and retry hydration for
# empty text rows before they are sent to Django history/live callbacks.
RUN node scripts/patch-hosted-message-hydration.js
# Accept authenticated binary uploads from Django for agent-sent photo/video/
# document attachments without exposing the gateway publicly.
RUN node scripts/patch-hosted-uploaded-media-send.js
# Fix URL-backed follow-up media MIME handling, expose authenticated on-demand
# message media downloads, and drop WhatsApp Status/broadcast callbacks.
RUN node scripts/patch-hosted-media-download-and-filter.js
# Apply last: serialize lifecycle transitions, verify owned browser exit before
# releasing leases, and back off repeated initialization failures.
RUN node scripts/patch-hosted-process-lifecycle.js
RUN node --check src/session-lifecycle.js && node --check src/index.js

# npm is required only while assembling node_modules. The production gateway
# runs directly with Node and never invokes npm/npx, so remove the package
# manager and its bundled dependency tree from the runtime image.
RUN rm -rf /usr/local/lib/node_modules/npm \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx

RUN mkdir -p /data/wwebjs_auth && chown -R node:node /app /data/wwebjs_auth
USER node

EXPOSE 3000
STOPSIGNAL SIGTERM

# Do not run the gateway through `npm start`: Docker signals PID 1, and npm's
# child process forwarding is not reliable enough for session-lease cleanup.
# Compose init reaps orphaned Chromium descendants and forwards signals.
# The shell exists only for one-time cleanup; exec makes Node its direct child.
# Standalone docker run must likewise use --init.
CMD ["sh", "-c", "node scripts/cleanup-chromium-profile-locks.js && exec node src/index.js"]
