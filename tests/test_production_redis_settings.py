import json
import os
import subprocess
import sys


REDIS_OVERRIDE_KEYS = (
    "CACHE_REDIS_URL",
    "CHANNEL_LAYER_REDIS_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
)


def _load_prod_redis_settings(*, overrides=None):
    env = os.environ.copy()
    env.update(
        {
            "SECRET_KEY": "redis-settings-test-secret",
            "DEBUG": "False",
            "ALLOWED_HOSTS": "localhost",
            "REDIS_URL": "redis://redis.example:6379/9",
        }
    )
    for key in REDIS_OVERRIDE_KEYS:
        env.pop(key, None)
    env.update(overrides or {})

    script = r'''
import json
import config.settings.prod as settings

print(json.dumps({
    "cache": settings.CACHES["default"]["LOCATION"],
    "channels": settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"][0],
    "broker": settings.CELERY_BROKER_URL,
    "results": settings.CELERY_RESULT_BACKEND,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_production_redis_defaults_use_separate_logical_databases():
    values = _load_prod_redis_settings()

    assert values == {
        "cache": "redis://redis.example:6379/0",
        "channels": "redis://redis.example:6379/1",
        "broker": "redis://redis.example:6379/2",
        "results": "redis://redis.example:6379/3",
    }


def test_production_redis_explicit_overrides_are_preserved():
    values = _load_prod_redis_settings(
        overrides={
            "CACHE_REDIS_URL": "rediss://cache.example:6380/4",
            "CHANNEL_LAYER_REDIS_URL": "rediss://channels.example:6380/5",
            "CELERY_BROKER_URL": "rediss://broker.example:6380/6",
            "CELERY_RESULT_BACKEND": "rediss://results.example:6380/7",
        }
    )

    assert values == {
        "cache": "rediss://cache.example:6380/4",
        "channels": "rediss://channels.example:6380/5",
        "broker": "rediss://broker.example:6380/6",
        "results": "rediss://results.example:6380/7",
    }
