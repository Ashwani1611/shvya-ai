from urllib.parse import urlsplit, urlunsplit

from decouple import config

from .base import *  # noqa


APP_ENV = "production"

INSTALLED_APPS = [
    *INSTALLED_APPS,
    "django.contrib.postgres",
    "apps.hosted_automation",
]


def _redis_db_url(base_url, db_index):
    """Reuse the configured Redis server while isolating logical keyspaces."""
    parsed = urlsplit(str(base_url or ""))
    if parsed.scheme not in {"redis", "rediss"} or not parsed.netloc:
        raise ValueError(
            "REDIS_URL must be a redis:// or rediss:// URL when production "
            "Redis DB URLs are derived automatically."
        )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{int(db_index)}",
            parsed.query,
            parsed.fragment,
        )
    )


# Keep independent Redis logical databases for unrelated runtime concerns.
# This prevents cache flushes or key maintenance from touching Celery broker
# state, result metadata, or Channels pub/sub keys. Every URL remains
# individually overrideable so a future deployment can move any concern to a
# fully separate Redis instance without changing code.
CACHE_REDIS_URL = config(
    "CACHE_REDIS_URL",
    default=_redis_db_url(REDIS_URL, 0),
)
CHANNEL_LAYER_REDIS_URL = config(
    "CHANNEL_LAYER_REDIS_URL",
    default=_redis_db_url(REDIS_URL, 1),
)
CELERY_BROKER_URL = config(
    "CELERY_BROKER_URL",
    default=_redis_db_url(REDIS_URL, 2),
)
CELERY_RESULT_BACKEND = config(
    "CELERY_RESULT_BACKEND",
    default=_redis_db_url(REDIS_URL, 3),
)

CACHES = {
    **CACHES,
    "default": {
        **CACHES["default"],
        "LOCATION": CACHE_REDIS_URL,
    },
}

CHANNEL_LAYERS = {
    **CHANNEL_LAYERS,
    "default": {
        **CHANNEL_LAYERS["default"],
        "CONFIG": {
            **CHANNEL_LAYERS["default"]["CONFIG"],
            "hosts": [CHANNEL_LAYER_REDIS_URL],
        },
    },
}

# The whatsapp-web.js gateway fetches short-lived signed follow-up media and
# posts authenticated session/message callbacks to Gunicorn over the private
# Docker network. Permit only the Docker service host in addition to the
# public hosts already supplied by the environment.
if "web" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "web"]

# These internal gateway routes intentionally stay HTTP inside the private
# Docker network. The event endpoint is protected by WHATSAPP_WEB_CALLBACK_TOKEN
# and the media endpoint uses signed URLs. Public browser traffic is still
# forced to HTTPS by SecurityMiddleware.
SECURE_REDIRECT_EXEMPT = [
    r"^dashboard/whatsapp/connect/hosted/events/$",
    r"^dashboard/whatsapp/connect/hosted/media/",
]

# Instagram API with Instagram Login exposes a dedicated Instagram App ID and
# App Secret in Meta's Instagram API setup. They are not interchangeable with
# the generic Meta App ID used by WhatsApp Embedded Signup. Production requires
# the dedicated pair so SHVYA never redirects customers to Instagram with the
# wrong client_id.
META_INSTAGRAM_APP_ID = config(
    "META_INSTAGRAM_APP_ID",
    default="",
)
META_INSTAGRAM_APP_SECRET = config(
    "META_INSTAGRAM_APP_SECRET",
    default="",
)
META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS = True

# Reuse healthy PostgreSQL connections across Gunicorn requests instead of
# paying connection setup cost on every request. Keep the lifetime tunable
# for deployments that later introduce PgBouncer.
DATABASES["default"]["CONN_MAX_AGE"] = config(
    "DB_CONN_MAX_AGE",
    default=60,
    cast=int,
)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

MIDDLEWARE = [
    *MIDDLEWARE,
    "apps.core.middleware.GlobalToastMiddleware",
    "apps.core.whatsapp_theme.WhatsAppThemeMiddleware",
]
