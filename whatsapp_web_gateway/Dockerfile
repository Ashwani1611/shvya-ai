FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ffmpeg ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package*.json ./
RUN npm install --omit=dev

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
# Expire abandoned QR sessions, clean their unpaired LocalAuth profile, release
# their lease, and expose per-state counts from the gateway health endpoint.
RUN node scripts/patch-hosted-qr-expiry.js
# Preserve the original whatsapp-web.js message type and retry hydration for
# empty text rows before they are sent to Django history/live callbacks.
RUN node scripts/patch-hosted-message-hydration.js
RUN node --check src/index.js

RUN mkdir -p /data/wwebjs_auth && chown -R node:node /app /data/wwebjs_auth
USER node

EXPOSE 3000
STOPSIGNAL SIGTERM

# Do not run the gateway through `npm start`: Docker signals PID 1, and npm's
# child process forwarding is not reliable enough for session-lease cleanup.
# The shell exists only for the one-time cleanup; `exec` replaces it with Node,
# making src/index.js PID 1 so its SIGTERM handler always receives Docker stop.
CMD ["sh", "-c", "node scripts/cleanup-chromium-profile-locks.js && exec node src/index.js"]
