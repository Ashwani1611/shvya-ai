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
RUN node --check src/index.js

RUN mkdir -p /data/wwebjs_auth && chown -R node:node /app /data/wwebjs_auth
USER node

EXPOSE 3000
CMD ["npm", "start"]
