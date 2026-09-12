FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
       curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Serve the Channels ASGI application so HTTP and WebSocket traffic share the
# same production process. The previous WSGI-only Gunicorn entrypoint could not
# accept /ws/whatsapp/... connections, which made Connect API chats require a
# reload even though the Redis channel layer and consumers were configured.
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
