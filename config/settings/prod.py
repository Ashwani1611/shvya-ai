from decouple import config

from .base import *  # noqa


INSTALLED_APPS = [
    *INSTALLED_APPS,
    "django.contrib.postgres",
    "apps.hosted_automation",
]

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
