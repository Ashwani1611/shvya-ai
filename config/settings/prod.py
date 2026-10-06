from urllib.parse import urlsplit, urlunsplit

from decouple import config
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa


APP_ENV = "production"

# Canonical externally advertised origin for the Operations MCP/OAuth server.
# This is intentionally pinned in production instead of trusting request Host.
OPERATIONS_PUBLIC_BASE_URL = "https://shvya-ai.com"

if OPERATIONS_PUBLIC_ORIGIN != "https://shvya-ai.com":
    raise ImproperlyConfigured(
        "OPERATIONS_PUBLIC_ORIGIN must be https://shvya-ai.com "
        "for production Operations MCP/OAuth metadata."
    )

# ---------------------------------------------------------------------------
# Private AWS S3 media storage
#
# Normal Django FileField uploads use S3 when enabled. SHVYA runs on a
# Hostinger VPS, so boto3 reads AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY from
# the server environment. Never commit those credentials to the repository.
# Static files continue to be served by Nginx.
#
# Encrypted Help & Support attachments intentionally keep using their dedicated
# FileSystemStorage in apps/support/storage.py and the persistent MEDIA_ROOT
# mount; changing Django's default storage does not alter that encrypted store.
# ---------------------------------------------------------------------------
AWS_STORAGE_BUCKET_NAME = str(
    config("AWS_STORAGE_BUCKET_NAME", default="") or ""
).strip()
AWS_S3_REGION_NAME = str(
    config("AWS_S3_REGION_NAME", default="ap-south-1") or "ap-south-1"
).strip()
AWS_S3_MEDIA_PREFIX = str(
    config("AWS_S3_MEDIA_PREFIX", default="media") or "media"
).strip().strip("/")
AWS_S3_PUBLIC_ASSET_PREFIX = str(
    config(
        "AWS_S3_PUBLIC_ASSET_PREFIX",
        default="production/media/public-assets",
    )
    or "production/media/public-assets"
).strip().strip("/")
AWS_QUERYSTRING_EXPIRE = config(
    "AWS_QUERYSTRING_EXPIRE",
    default=900,
    cast=int,
)
USE_S3_STORAGE = config(
    "USE_S3_STORAGE",
    default=bool(AWS_STORAGE_BUCKET_NAME),
    cast=bool,
)
USE_S3_PUBLIC_ASSETS = config(
    "USE_S3_PUBLIC_ASSETS",
    default=USE_S3_STORAGE,
    cast=bool,
)
AWS_PUBLIC_ASSET_QUERYSTRING_EXPIRE = config(
    "AWS_PUBLIC_ASSET_QUERYSTRING_EXPIRE",
    default=86400,
    cast=int,
)

if USE_S3_PUBLIC_ASSETS and not USE_S3_STORAGE:
    raise ImproperlyConfigured(
        "USE_S3_PUBLIC_ASSETS requires USE_S3_STORAGE=True."
    )

if USE_S3_STORAGE:
    if not AWS_STORAGE_BUCKET_NAME:
        raise ImproperlyConfigured(
            "AWS_STORAGE_BUCKET_NAME is required when USE_S3_STORAGE=True."
        )
    if AWS_QUERYSTRING_EXPIRE < 60:
        raise ImproperlyConfigured(
            "AWS_QUERYSTRING_EXPIRE must be at least 60 seconds."
        )

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": AWS_STORAGE_BUCKET_NAME,
                "region_name": AWS_S3_REGION_NAME,
                "location": AWS_S3_MEDIA_PREFIX,
                "default_acl": None,
                "file_overwrite": False,
                "querystring_auth": True,
                "querystring_expire": AWS_QUERYSTRING_EXPIRE,
                "signature_version": "s3v4",
                "addressing_style": "virtual",
                "object_parameters": {
                    "ServerSideEncryption": "AES256",
                },
            },
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

    if USE_S3_PUBLIC_ASSETS:
        STORAGES["public_assets"] = {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": AWS_STORAGE_BUCKET_NAME,
                "region_name": AWS_S3_REGION_NAME,
                "location": AWS_S3_PUBLIC_ASSET_PREFIX,
                "default_acl": None,
                "file_overwrite": True,
                "querystring_auth": True,
                "querystring_expire": AWS_PUBLIC_ASSET_QUERYSTRING_EXPIRE,
                "signature_version": "s3v4",
                "addressing_style": "virtual",
                "object_parameters": {
                    "ServerSideEncryption": "AES256",
                    "CacheControl": "public,max-age=31536000,immutable",
                },
            },
        }


# Production security is explicit and must not inherit DEBUG-dependent values
# calculated in base.py. Even if the deployment environment accidentally sets
# DEBUG=True, production remains fail-closed instead of disabling HTTPS,
# secure cookies, HSTS, or broadening CORS.
DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
CORS_ALLOW_ALL_ORIGINS = False

# Public signup verification and CRM password-reset email must leave the server
# in production. Support both canonical Django EMAIL_* names and SHVYA's
# historical SMTP_* environment names during migration.
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = config(
    "EMAIL_HOST",
    default=config("SMTP_HOST", default=EMAIL_HOST),
)
_email_port = str(config("EMAIL_PORT", default="") or "").strip()
_smtp_port = str(config("SMTP_PORT", default="") or "").strip()
EMAIL_PORT = int(_email_port or _smtp_port or EMAIL_PORT)
EMAIL_HOST_USER = config(
    "EMAIL_HOST_USER",
    default=config("SMTP_USERNAME", default=EMAIL_HOST_USER),
)
EMAIL_HOST_PASSWORD = config(
    "EMAIL_HOST_PASSWORD",
    default=config("SMTP_PASSWORD", default=EMAIL_HOST_PASSWORD),
)

# Production fails closed unless signing and recoverable-provider credentials
# use secrets distinct from Django's SECRET_KEY. Deploy workflows provision
# these values server-side before application containers start.
JWT_SECRET = str(config("JWT_SECRET", default="") or "").strip()
CREDENTIAL_ENCRYPTION_KEY = str(
    config("CREDENTIAL_ENCRYPTION_KEY", default="") or ""
).strip()
if not JWT_SECRET:
    raise ImproperlyConfigured("JWT_SECRET is required in production.")
if not CREDENTIAL_ENCRYPTION_KEY:
    raise ImproperlyConfigured(
        "CREDENTIAL_ENCRYPTION_KEY is required in production."
    )
if JWT_SECRET == SECRET_KEY:
    raise ImproperlyConfigured(
        "JWT_SECRET must be distinct from SECRET_KEY in production."
    )
if CREDENTIAL_ENCRYPTION_KEY == SECRET_KEY:
    raise ImproperlyConfigured(
        "CREDENTIAL_ENCRYPTION_KEY must be distinct from SECRET_KEY in production."
    )

SIMPLE_JWT = {
    **SIMPLE_JWT,
    "SIGNING_KEY": JWT_SECRET,
}

INSTALLED_APPS = [
    *INSTALLED_APPS,
    "django.contrib.postgres",
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


def _isolated_celery_url(setting_name, db_index):
    """Migrate legacy shared-/0 Celery URLs while preserving real overrides."""
    configured = str(config(setting_name, default="") or "").strip()
    base_url = str(REDIS_URL or "").strip()

    # Older SHVYA deployments commonly set CELERY_* to exactly REDIS_URL (/0).
    # Treat that as the legacy shared-keyspace configuration and migrate it to
    # the dedicated DB automatically. A genuinely different endpoint/backend
    # remains an explicit operator override and is preserved.
    if configured and configured != base_url:
        return configured
    return _redis_db_url(base_url, db_index)


# Keep independent Redis logical databases for unrelated runtime concerns.
# This prevents cache flushes or key maintenance from touching Celery broker
# state, result metadata, or Channels pub/sub keys. Cache/Channels URLs remain
# directly overrideable. Celery also preserves genuinely distinct legacy
# overrides while automatically migrating the old shared REDIS_URL value.
CACHE_REDIS_URL = config(
    "CACHE_REDIS_URL",
    default=_redis_db_url(REDIS_URL, 0),
)
CHANNEL_LAYER_REDIS_URL = config(
    "CHANNEL_LAYER_REDIS_URL",
    default=_redis_db_url(REDIS_URL, 1),
)
CELERY_BROKER_URL = _isolated_celery_url("CELERY_BROKER_URL", 2)
CELERY_RESULT_BACKEND = _isolated_celery_url("CELERY_RESULT_BACKEND", 3)

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
# Load the existing environment key; otherwise the webhook getter silently
# falls back to the unrelated generic Meta/WhatsApp verification token.
META_INSTAGRAM_VERIFY_TOKEN = config("META_INSTAGRAM_VERIFY_TOKEN", default="")
META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS = True

# Reuse healthy PostgreSQL connections across Gunicorn requests instead of
# paying connection setup cost on every request. Keep the lifetime tunable
# for deployments that later introduce PgBouncer.
DATABASES["default"]["CONN_MAX_AGE"] = config(
    "DB_CONN_MAX_AGE",
    # Transaction-pooled PgBouncer should use 0. Direct PostgreSQL deployments
    # may explicitly choose a small positive lifetime for WSGI workers.
    default=0 if config("DB_USE_PGBOUNCER", default=True, cast=bool) else 60,
    cast=int,
)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DB_USE_PGBOUNCER = config("DB_USE_PGBOUNCER", default=True, cast=bool)
if DB_USE_PGBOUNCER:
    # QuerySet.iterator() server-side cursors can outlive one transaction and
    # are therefore incompatible with PgBouncer transaction pooling.
    DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True

MIDDLEWARE = [
    *MIDDLEWARE,
    "apps.core.middleware.GlobalToastMiddleware",
    "apps.core.whatsapp_theme.WhatsAppThemeMiddleware",
]
