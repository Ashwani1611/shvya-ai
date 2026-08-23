"""
Django settings for SHVYA AI — Phase 1
Foundation: Organization, User, Pipeline, Stage, Lead on PostgreSQL.
"""

from pathlib import Path
from datetime import timedelta

from decouple import config


BASE_DIR = Path(__file__).resolve().parent.parent


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
# Applications
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
    "corsheaders",

    # SHVYA apps — Phase 1
    "apps.organizations",
    "apps.accounts",
    "apps.crm",
    "apps.superadmin",
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
#
# The CRM uses:
#
#     /dashboard/login/
#
# for its dedicated login page.
#
# The CRM authentication session is isolated from:
#
#     /admin/
#     /superadmin/
#
# and uses the dedicated:
#
#     shvya_crm_sessionid
#
# cookie.
# ---------------------------------------------------------------------------

CRM_LOGIN_URL = "/dashboard/login/"


# ---------------------------------------------------------------------------
# Password Reset
# ---------------------------------------------------------------------------
#
# Django's built-in password reset functionality uses this timeout
# for password-reset tokens.
#
# 3600 seconds = 1 hour.
#
# The token automatically becomes invalid after this period.
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
# Example:
#
#     python manage.py runserver
#
# Then when a user requests:
#
#     /dashboard/login/
#
# -> Forgot Password
#
# Django will print the password-reset email and reset URL in the
# terminal window.
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
#
# Used as the "From" address for password-reset emails.
#
# In local development this value is only used as the sender displayed
# inside the console-generated email.
#
# Later this can be changed to:
#
#     noreply@shvya.ai
#
# or another verified SHVYA email address.
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
#
# The project-level templates directory is intentionally included here.
# This allows us to override Django Admin templates from:
#
#     templates/admin/
#
# It also supports:
#
#     templates/superadmin/
#     templates/crm/
#     etc.
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
                "core.context_processors.sidebar_nav",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database — PostgreSQL
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
# Custom user model — multi-tenant
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
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),

    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),

    "DEFAULT_PAGINATION_CLASS": (
        "rest_framework.pagination.LimitOffsetPagination"
    ),

    "PAGE_SIZE": 25,
}


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        hours=8,
    ),

    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=7,
    ),

    "ROTATE_REFRESH_TOKENS": True,
}


CORS_ALLOW_ALL_ORIGINS = DEBUG


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