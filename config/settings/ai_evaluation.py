"""Recorded AI evaluations: test database, process-local cache/queues and media.

The full CI suite still uses testing.py with real Redis. This settings module is
only the management command's subprocess boundary; it must not clear or publish
to a developer's or production worker's configured cache/channel/broker.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile

from .testing import *  # noqa: F401,F403

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                      "LOCATION": "shvya-recorded-ai-evaluation"}}
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
# Direct Redis lock clients must be mocked by recorded scenarios, never fall
# through to the inherited live endpoint. Unexpected direct calls fail closed.
REDIS_URL = ""
CHANNEL_LAYER_REDIS_URL = ""
OPENAI_API_KEY = ""
DATABASES = deepcopy(DATABASES)  # noqa: F405
for alias, database in DATABASES.items():
    identity = f"{alias}:{database.get('HOST', '')}:{database.get('NAME', '')}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    database["TEST"] = {"NAME": f"test_shvya_ai_eval_{digest}"}
MEDIA_ROOT = Path(os.environ.get("SHVYA_AI_EVALUATION_MEDIA_ROOT") or
                  str(Path(tempfile.gettempdir()) / "shvya-recorded-ai-evaluation-media"))
