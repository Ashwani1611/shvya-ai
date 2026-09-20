import json
import os
import subprocess
import sys


def _load_prod_email_settings(*, overrides=None):
    env = os.environ.copy()
    env.update(
        {
            "SECRET_KEY": "email-settings-django-secret",
            "JWT_SECRET": "email-settings-jwt-secret",
            "CREDENTIAL_ENCRYPTION_KEY": "email-settings-credential-secret",
            "DEBUG": "False",
            "ALLOWED_HOSTS": "dashboard.shvya-ai.com",
            "REDIS_URL": "redis://localhost:6379/0",
        }
    )
    for key in (
        "EMAIL_HOST",
        "EMAIL_PORT",
        "EMAIL_HOST_USER",
        "EMAIL_HOST_PASSWORD",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
    ):
        env.pop(key, None)
    env.update(overrides or {})

    script = r"""
import json
import config.settings.prod as settings

print(json.dumps({
    "host": settings.EMAIL_HOST,
    "port": settings.EMAIL_PORT,
    "user": settings.EMAIL_HOST_USER,
    "password": settings.EMAIL_HOST_PASSWORD,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_blank_legacy_smtp_values_are_treated_as_unset():
    values = _load_prod_email_settings(
        overrides={
            "SMTP_HOST": "",
            "SMTP_PORT": "",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
        }
    )

    assert values["host"] == "smtp.gmail.com"
    assert values["port"] == 587
    assert values["user"] == ""
    assert values["password"] == ""


def test_canonical_email_settings_override_legacy_smtp_values():
    values = _load_prod_email_settings(
        overrides={
            "EMAIL_HOST": "smtp.example.com",
            "EMAIL_PORT": "2525",
            "EMAIL_HOST_USER": "mailer",
            "EMAIL_HOST_PASSWORD": "secret",
            "SMTP_HOST": "legacy.example.com",
            "SMTP_PORT": "25",
            "SMTP_USERNAME": "legacy-user",
            "SMTP_PASSWORD": "legacy-secret",
        }
    )

    assert values == {
        "host": "smtp.example.com",
        "port": 2525,
        "user": "mailer",
        "password": "secret",
    }
