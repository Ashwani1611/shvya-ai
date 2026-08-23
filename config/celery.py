"""
Celery config for SHVYA AI.

Not wired into INSTALLED_APPS yet — Phase 1 has no async work.
Activated in Phase 5 (Automation).
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("shvya")

app.config_from_object(
    "django.conf:settings",
    namespace="CELERY",
)

app.autodiscover_tasks()