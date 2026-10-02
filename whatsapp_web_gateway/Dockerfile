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
# Session recovery, bounded Chromium teardown, failed-session backoff and
# stale Chromium lock cleanup are integrated directly in src/index.js.
# Keep the lease-invariant verification patch as a build-time safety check.
RUN node scripts/patch-hosted-lease-invariant.js
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
# Durable live callbacks, disconnected-session recovery, and server-ACK sends.
RUN node scripts/patch-hosted-realtime-delivery.js
RUN node --check src/index.js

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
# The shell exists only for the one-time cleanup; `exec` replaces it with Node,
# making src/index.js PID 1 so its SIGTERM handler always receives Docker stop.
CMD ["sh", "-c", "node scripts/cleanup-chromium-profile-locks.js && exec node src/index.js"]
