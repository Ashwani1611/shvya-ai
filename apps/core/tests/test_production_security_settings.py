import json
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase


class ProductionSettingsSecurityTests(SimpleTestCase):
    def _load_security_settings(self, module_name):
        project_root = Path(__file__).resolve().parents[3]
        code = f"""
import importlib
import json

settings = importlib.import_module({module_name!r})
print(json.dumps({{
    "debug": settings.DEBUG,
    "session_cookie_secure": settings.SESSION_COOKIE_SECURE,
    "csrf_cookie_secure": settings.CSRF_COOKIE_SECURE,
    "ssl_redirect": settings.SECURE_SSL_REDIRECT,
    "hsts_seconds": settings.SECURE_HSTS_SECONDS,
    "hsts_subdomains": settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
    "hsts_preload": settings.SECURE_HSTS_PRELOAD,
    "cors_allow_all": settings.CORS_ALLOW_ALL_ORIGINS,
    "operations_public_base_url": settings.OPERATIONS_PUBLIC_BASE_URL,
}}))
"""
        env = os.environ.copy()
        env.update(
            {
                "SECRET_KEY": "production-settings-security-test",
                "DEBUG": "True",
                "ALLOWED_HOSTS": "dashboard.shvya-ai.com",
                "REDIS_URL": "redis://localhost:6379/0",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=project_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_production_and_staging_fail_closed_when_env_requests_debug(self):
        for module_name in (
            "config.settings.prod",
            "config.settings.staging",
        ):
            with self.subTest(module_name=module_name):
                values = self._load_security_settings(module_name)

                self.assertFalse(values["debug"])
                self.assertTrue(values["session_cookie_secure"])
                self.assertTrue(values["csrf_cookie_secure"])
                self.assertTrue(values["ssl_redirect"])
                self.assertEqual(values["hsts_seconds"], 31536000)
                self.assertTrue(values["hsts_subdomains"])
                self.assertTrue(values["hsts_preload"])
                self.assertFalse(values["cors_allow_all"])
                expected_operations_origin = {
                    "config.settings.prod": "https://dashboard.shvya-ai.com",
                    "config.settings.staging": "https://staging.shvya-ai.com",
                }[module_name]
                self.assertEqual(
                    values["operations_public_base_url"],
                    expected_operations_origin,
                )
