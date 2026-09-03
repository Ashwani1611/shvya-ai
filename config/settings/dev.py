from .base import *

DEBUG = True

CORS_ALLOW_ALL_ORIGINS = True

# ---------------------------------------------------------------------------
# Celery -- run tasks synchronously in local dev
# ---------------------------------------------------------------------------
# No Celery worker process is started by `manage.py runserver`, so without
# this, `.delay()` calls (e.g. sending a WhatsApp message) either raise if
# Redis isn't running, or silently queue forever if it is but nothing is
# consuming the queue. Eager mode runs the task inline instead, matching
# what a worker would do, so message sending works out of the box locally.
# Docker/staging/prod are unaffected -- they use the real `worker` service.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True