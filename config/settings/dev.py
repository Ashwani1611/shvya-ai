from .base import *

DEBUG = True

CORS_ALLOW_ALL_ORIGINS = True

# ---------------------------------------------------------------------------
# Celery -- run tasks synchronously in local dev
# ---------------------------------------------------------------------------
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# ---------------------------------------------------------------------------
# Security hardening -- base.py computes these from DEBUG *before* this
# file's `DEBUG = True` override takes effect, so they stay locked to
# whatever base.py resolved (often True, since .env's DEBUG is usually
# left blank per docs/local_setup.md). That forces every request onto
# HTTPS, which the local dev server doesn't serve -- the admin page (and
# every other page) just never loads. Explicitly reset them here.
# ---------------------------------------------------------------------------
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False