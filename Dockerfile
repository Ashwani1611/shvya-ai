FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

ARG APP_UID=10001
ARG APP_GID=10001

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
       curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" shvya \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin shvya

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=${APP_UID}:${APP_GID} . .
RUN mkdir -p /app/staticfiles /app/media \
    && chown -R "${APP_UID}:${APP_GID}" /app

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
