import tempfile

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name
from apps.organizations.models import Organization
from apps.vault.services import create_vault


class VaultTestCase(TestCase):
    """Real organizations and isolated private storage for all Vault tests."""

    def setUp(self):
        super().setUp()
        self.storage = tempfile.TemporaryDirectory(prefix="shvya-vault-test-")
        self.addCleanup(self.storage.cleanup)
        self.settings_override = override_settings(
            VAULT_STORAGE_ROOT=self.storage.name,
            ALLOWED_HOSTS=["testserver"],
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)
        self.org = Organization.objects.create(name="Vault Client Alpha")
        self.other_org = Organization.objects.create(name="Vault Client Beta")
        self.vault, self.code = create_vault(self.org)
        self.other_vault, self.other_code = create_vault(self.other_org)

    def superadmin(self):
        return User.objects.create_user(
            email="vault-owner@example.test",
            password="test-only-password",
            name="Vault operator",
            role=User.Role.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
        )

    def area_login(self, user, client=None, area="superadmin"):
        client = client or self.client
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.save()
        client.cookies[get_session_cookie_name(area)] = session.session_key
        return session
