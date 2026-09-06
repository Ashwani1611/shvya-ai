"""
Settings used when running the test suite (pytest).

Activate with:
    DJANGO_SETTINGS_MODULE=config.settings.testing pytest
"""
from .base import *  # noqa: F401,F403

DEBUG = False

INSTALLED_APPS = [
    *INSTALLED_APPS,
    "django.contrib.postgres",
    "apps.hosted_automation",
]

# Emails go to memory during tests, never real SMTP.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Fast, insecure password hasher -- speeds up user-creation-heavy tests.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Celery tasks run synchronously and eagerly during tests -- no worker/broker needed.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True


SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
