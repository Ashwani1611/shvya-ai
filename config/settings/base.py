"""
Django settings for SHVYA AI.

Shared base settings -- imported by:

    config/settings/local.py       (development)
    config/settings/production.py  (production)

Do not import this module directly as DJANGO_SETTINGS_MODULE.
"""

from pathlib import Path
from datetime import timedelta

from decouple import config


# ---------------------------------------------------------------------------
# This file lives at config/settings/base.py, so BASE_DIR needs THREE
# .parent calls to reach the project root (not two).
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

SECRET_KEY = config("SECRET_KEY")

DEBUG = config(
    "DEBUG",
    default=False,
    cast=bool,
)

ALLOWED_HOSTS = config(
    "ALLOWED_HOSTS",
    default="localhost,127.0.0.1",
).split(",")


# ---------------------------------------------------------------------------
# OpenAI
#
# Provider configuration is centralized here so all AI services use the
# same Django settings layer rather than reading environment variables
# independently.
# ---------------------------------------------------------------------------

OPENAI_API_KEY = config(
    "OPENAI_API_KEY",
    default="",
)

OPENAI_AI_MODEL = config(
    "OPENAI_AI_MODEL",
    default="gpt-4.1-nano",
)

OPENAI_EMBEDDING_MODEL = config(
    "OPENAI_EMBEDDING_MODEL",
    default="text-embedding-3-small",
)


# ---------------------------------------------------------------------------
# Security hardening
#
# No-ops under DEBUG=True (local dev over plain HTTP); activate
# automatically once DEBUG=False in a real deployment.
# CSRF_TRUSTED_ORIGINS must be set explicitly per-environment, e.g.
# CSRF_TRUSTED_ORIGINS=https://app.shvya.ai in that environment's .env.
# ---------------------------------------------------------------------------

SESSION_COOKIE_SECURE = not DEBUG

CSRF_COOKIE_SECURE = not DEBUG

SECURE_SSL_REDIRECT = not DEBUG

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0

SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG

SECURE_HSTS_PRELOAD = not DEBUG

SECURE_CONTENT_TYPE_NOSNIFF = True

X_FRAME_OPTIONS = "DENY"

CSRF_TRUSTED_ORIGINS = [
    origin
    for origin in config(
        "CSRF_TRUSTED_ORIGINS",
        default="",
    ).split(",")
    if origin
]


# ---------------------------------------------------------------------------
# Cache -- Redis. Used for login rate limiting (core.utils) and will be
# used by every future app needing caching, instead of each app wiring
# its own cache client.
#
# Native Django Redis backend (Django 4+) -- no extra package needed,
# `redis` is already a dependency for Celery.
# ---------------------------------------------------------------------------

REDIS_URL = config(
    "REDIS_URL",
    default="redis://localhost:6379/0",
)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    },
}

# Celery -- uses the same Redis instance as the cache above.
CELERY_BROKER_URL = config(
    "CELERY_BROKER_URL",
    default=REDIS_URL,
)

CELERY_RESULT_BACKEND = config(
    "CELERY_RESULT_BACKEND",
    default=REDIS_URL,
)

CELERY_ACCEPT_CONTENT = ["json"]

CELERY_TASK_SERIALIZER = "json"

CELERY_RESULT_SERIALIZER = "json"

CELERY_TIMEZONE = "Asia/Kolkata"


# ---------------------------------------------------------------------------
# Applications
#
# Grouped by domain to stay readable as the app count grows. Every new
# feature app gets added under its matching domain block below --
# don't add feature apps to the "Core platform" group.
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",

    # --- Core & Platform Apps ---
    "apps.organizations",
    "apps.accounts",
    "apps.crm",
    "apps.superadmin",

    # --- Business & AI Feature Apps ---
    "apps.channels",
    "apps.calls",
    "apps.copilot",
    "apps.ai_engagement",
    "apps.triggers",
    "apps.followups",
    "apps.analytics",
    "apps.integrations",
    "apps.teams",
    "apps.telephony",
]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.SHVYAAreaAuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ---------------------------------------------------------------------------
# URL / Application entry points
# ---------------------------------------------------------------------------

ROOT_URLCONF = "config.urls"

WSGI_APPLICATION = "config.wsgi.application"

ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Authentication / Login
#
# Django's @login_required redirects unauthenticated users here.
#
# IMPORTANT:
#
# SHVYA CRM itself uses its own dedicated authentication system and
# /dashboard/login/ endpoint.
# ---------------------------------------------------------------------------

LOGIN_URL = "/superadmin/login/"


# ---------------------------------------------------------------------------
# CRM Authentication
# ---------------------------------------------------------------------------

CRM_LOGIN_URL = "/dashboard/login/"


# ---------------------------------------------------------------------------
# Password Reset
# ---------------------------------------------------------------------------

PASSWORD_RESET_TIMEOUT = 3600


# ---------------------------------------------------------------------------
# Email Configuration
# ---------------------------------------------------------------------------
#
# DEVELOPMENT MODE
#
# During local development, password-reset emails are printed directly
# into the Django development server terminal.
#
# This allows the complete password-reset flow to be tested without
# configuring an external SMTP provider yet.
# ---------------------------------------------------------------------------

EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)


# ---------------------------------------------------------------------------
# Email Sender
# ---------------------------------------------------------------------------

DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL",
    default="noreply@shvya.ai",
)


# ---------------------------------------------------------------------------
# SMTP Configuration
# ---------------------------------------------------------------------------
#
# These settings are prepared for production email delivery.
#
# They are intentionally environment-driven so credentials are NOT
# hard-coded into the repository.
#
# When EMAIL_BACKEND is changed to:
#
#     django.core.mail.backends.smtp.EmailBackend
#
# these values will be used.
# ---------------------------------------------------------------------------

EMAIL_HOST = config(
    "EMAIL_HOST",
    default="smtp.gmail.com",
)

EMAIL_PORT = config(
    "EMAIL_PORT",
    default=587,
    cast=int,
)

EMAIL_USE_TLS = config(
    "EMAIL_USE_TLS",
    default=True,
    cast=bool,
)

EMAIL_USE_SSL = config(
    "EMAIL_USE_SSL",
    default=False,
    cast=bool,
)

EMAIL_HOST_USER = config(
    "EMAIL_HOST_USER",
    default="",
)

EMAIL_HOST_PASSWORD = config(
    "EMAIL_HOST_PASSWORD",
    default="",
)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",

        "DIRS": [
            BASE_DIR / "templates",
        ],

        "APP_DIRS": True,

        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",

                # -----------------------------------------------------------
                # SHVYA Admin
                # Real PostgreSQL statistics for the Admin Control Center.
                # -----------------------------------------------------------
                "apps.superadmin.context_processors.shvya_admin_stats",

                # -----------------------------------------------------------
                # SHVYA Sidebar
                # Shared sidebar navigation items and Tabler icons.
                # -----------------------------------------------------------
                "apps.core.context_processors.sidebar_nav",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database -- PostgreSQL
# SHVYA does not use SQLite, even in development.
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",

        "NAME": config(
            "DB_NAME",
            default="shvya_db",
        ),

        "USER": config(
            "DB_USER",
            default="shvya_user",
        ),

        "PASSWORD": config(
            "DB_PASSWORD",
            default="shvya_pass",
        ),

        "HOST": config(
            "DB_HOST",
            default="localhost",
        ),

        "PORT": config(
            "DB_PORT",
            default="5432",
        ),
    }
}


# ---------------------------------------------------------------------------
# Custom user model -- multi-tenant
# User belongs to an Organization.
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]


# ---------------------------------------------------------------------------
# REST Framework + JWT
#
# DEFAULT_PAGINATION_CLASS and EXCEPTION_HANDLER point at core/ so
# every app -- current and future -- gets consistent pagination and
# error-response shape without repeating DRF boilerplate per app.
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),

    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),

    "DEFAULT_PAGINATION_CLASS": (
        "apps.core.pagination.StandardResultsPagination"
    ),

    "EXCEPTION_HANDLER": (
        "apps.core.exceptions.api_exception_handler"
    ),

    "PAGE_SIZE": 25,

    "DEFAULT_THROTTLE_RATES": {
        "jwt_login": "10/min",
    },
}


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        hours=8,
    ),

    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=7,
    ),

    "ROTATE_REFRESH_TOKENS": True,

    "BLACKLIST_AFTER_ROTATION": True,
}


CORS_ALLOW_ALL_ORIGINS = DEBUG


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

SESSION_COOKIE_AGE = 86400  # 1 day, per SHVYA's session requirement


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Asia/Kolkata"

USE_I18N = True

USE_TZ = True


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = "static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STATIC_ROOT = BASE_DIR / "staticfiles"


# ---------------------------------------------------------------------------
# Media files
# ---------------------------------------------------------------------------

MEDIA_URL = "media/"

MEDIA_ROOT = BASE_DIR / "media"


# ---------------------------------------------------------------------------
# Default primary key
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ============================================================
# WhatsApp (Meta) webhook
# ============================================================

# META_VERIFY_TOKEN: shared secret you choose yourself and enter
# into Meta's App Dashboard webhook config -- Meta echoes it back
# on the GET verification handshake. One value per SHVYA
# deployment, not per organization (Meta calls a single webhook
# URL regardless of how many orgs/numbers are connected).

# META_APP_SECRET: from Meta App Dashboard > Settings > Basic.
# Used to verify X-Hub-Signature-256 on incoming webhook POSTs so
# we can trust the payload actually came from Meta.

META_VERIFY_TOKEN = config(
    "META_VERIFY_TOKEN",
    default="",
)

META_APP_SECRET = config(
    "META_APP_SECRET",
    default="",
)


# ============================================================
# WhatsApp (Meta) Embedded Signup
# ============================================================

# Lets "Connect WhatsApp Now" open Meta's own Facebook-hosted
# onboarding popup (pick/create a WABA, verify the number, accept
# Meta's terms) instead of asking the person to paste a
# phone_number_id / access_token by hand. Both values below come
# from Meta App Dashboard, NOT something this codebase can
# generate on its own:
#
# META_APP_ID: App Dashboard > Settings > Basic > "App ID". Public
# -- this is fine to ship to the browser (it's rendered into the
# connect-api page to init the Facebook JS SDK).
#
# META_WA_EMBEDDED_SIGNUP_CONFIG_ID: App Dashboard > WhatsApp >
# Configuration > Embedded Signup > create a "Configuration" there
# (requires the app to have the WhatsApp product added, a
# connected Meta Business Manager, and -- for use with real
# customers rather than test numbers -- App Review approval for
# the whatsapp_business_management and whatsapp_business_messaging
# permissions). Until this is set, the connect-api page falls back
# to the manual phone_number_id / access_token form further down
# this same page.

META_APP_ID = config(
    "META_APP_ID",
    default="",
)

META_WA_EMBEDDED_SIGNUP_CONFIG_ID = config(
    "META_WA_EMBEDDED_SIGNUP_CONFIG_ID",
    default="",
)