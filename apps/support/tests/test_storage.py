"""Verify persistence and confidentiality using the real Django storage backend."""
import secrets
import tempfile
from pathlib import Path

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings

from apps.support.storage import MAGIC, PrivateSupportStorage


class SupportStorageTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = secrets.token_urlsafe(48)
        setting = override_settings(MEDIA_ROOT=self.directory.name, SUPPORT_PRIVATE_ROOT="",
                                   SECRET_KEY=self.key, SECRET_KEY_FALLBACKS=[])
        setting.enable()
        self.addCleanup(setting.disable)
        self.storage = PrivateSupportStorage()

    def test_restart_reads_persisted_encrypted_attachment(self):
        name = self.storage.save("org/ticket/random", ContentFile(b"private customer recording"))
        raw = Path(self.storage.path(name)).read_bytes()
        self.assertTrue(raw.startswith(MAGIC))
        self.assertNotIn(b"private customer recording", raw)
        restarted = PrivateSupportStorage()
        with restarted.open(name) as content:
            self.assertEqual(content.read(), b"private customer recording")
        self.assertEqual(restarted.size(name), len(b"private customer recording"))
        self.assertTrue(Path(restarted.location).is_relative_to(self.directory.name))

    def test_tampering_is_rejected(self):
        name = self.storage.save("sample", ContentFile(b"recording"))
        path = Path(self.storage.path(name))
        raw = path.read_bytes()
        path.write_bytes(raw[:-20] + b"x" * 20)
        with self.assertRaises(SuspiciousFileOperation):
            self.storage.open(name)

    def test_plaintext_is_never_accepted_as_encrypted_content(self):
        name = self.storage.save("sample", ContentFile(b"recording"))
        Path(self.storage.path(name)).write_bytes(b"unencrypted customer content")
        with self.assertRaises(SuspiciousFileOperation):
            self.storage.open(name)

    def test_rotation_preserves_files_only_with_old_key_available(self):
        name = self.storage.save("sample", ContentFile(b"recording"))
        with override_settings(SECRET_KEY=secrets.token_urlsafe(48), SECRET_KEY_FALLBACKS=[self.key]):
            with PrivateSupportStorage().open(name) as content:
                self.assertEqual(content.read(), b"recording")
        with override_settings(SECRET_KEY=secrets.token_urlsafe(48), SECRET_KEY_FALLBACKS=[]):
            with self.assertRaises(SuspiciousFileOperation):
                PrivateSupportStorage().open(name)

    def test_no_public_url_and_no_path_traversal(self):
        with self.assertRaises(ValueError):
            self.storage.url("sample")
        with self.assertRaises(SuspiciousFileOperation):
            self.storage.save("../escape", ContentFile(b"recording"))

    def test_no_file_write_when_over_hard_limit(self):
        from unittest.mock import patch
        with patch("apps.support.storage.MAX_PLAINTEXT", 3):
            with self.assertRaises(SuspiciousFileOperation):
                self.storage.save("large", ContentFile(b"1234"))
        self.assertFalse(self.storage.exists("large"))
