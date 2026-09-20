import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase


class ProductionSettingsTests(SimpleTestCase):
    def test_blank_legacy_smtp_port_falls_back_to_default_email_port(self):
        env = os.environ.copy()
        env.pop("EMAIL_PORT", None)
        env.update(
            {
                "SMTP_PORT": "",
                "SECRET_KEY": "test-secret-key",
                "JWT_SECRET": "test-jwt-secret",
                "CREDENTIAL_ENCRYPTION_KEY": "test-credential-key",
            }
        )
        project_root = Path(__file__).resolve().parents[3]

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from config.settings import prod; print(prod.EMAIL_PORT)",
            ],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "587")
