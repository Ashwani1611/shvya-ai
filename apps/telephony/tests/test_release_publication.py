import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings

from apps.telephony.models import CallAppRelease
from apps.telephony.releases import APK_PATH, APK_SHA256, publish_android_release
from apps.telephony.tests import test_mobile_workspace as workspace_fixtures


@override_settings(OPERATIONS_PUBLIC_BASE_URL="https://shvya-ai.com")
class AndroidReleasePublicationTests(TestCase):
    def setUp(self):
        self.previous = CallAppRelease.objects.create(
            version_name="1.5", version_code=9,
            download_url="https://shvya-ai.com/previous.apk", is_active=True,
        )
        self.binary = (Path(settings.BASE_DIR) / "static" / APK_PATH).read_bytes()
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.iter_content.side_effect = lambda **kwargs: iter([self.binary])
        self.get = patch("apps.telephony.releases.requests.get", return_value=self.response).start()
        self.addCleanup(patch.stopall)

    def test_publishes_exact_binary_idempotently_and_retains_history(self):
        self.assertEqual(hashlib.sha256(self.binary).hexdigest(), APK_SHA256)
        first = publish_android_release()
        second = publish_android_release()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CallAppRelease.objects.count(), 2)
        self.previous.refresh_from_db()
        self.assertFalse(self.previous.is_active)
        self.assertEqual(first.sha256, APK_SHA256)
        self.assertFalse(first.is_mandatory)
        self.assertIn("alongside", first.release_notes)
        self.assertEqual(first.download_url, f"https://shvya-ai.com/static/{APK_PATH}")

    def test_wrong_public_bytes_leave_current_download_unchanged(self):
        self.response.iter_content.side_effect = lambda **kwargs: iter([b"old APK"])
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            publish_android_release()
        self.previous.refresh_from_db()
        self.assertTrue(self.previous.is_active)
        self.assertEqual(CallAppRelease.objects.count(), 1)

    def test_http_failure_does_not_activate_release(self):
        self.response.raise_for_status.side_effect = RuntimeError("unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            publish_android_release()
        self.assertEqual(CallAppRelease.objects.filter(is_active=True).get(), self.previous)

    def test_version_collision_does_not_replace_different_binary(self):
        CallAppRelease.objects.create(
            version_name="other", version_code=10, sha256="f" * 64,
            download_url="https://shvya-ai.com/other.apk",
        )
        with self.assertRaisesRegex(ValueError, "another APK"):
            publish_android_release()
        self.previous.refresh_from_db()
        self.assertTrue(self.previous.is_active)

    def test_redeploy_never_downgrades_a_newer_active_release(self):
        newer = CallAppRelease.objects.create(
            version_name="2.0", version_code=11,
            download_url="https://shvya-ai.com/newer.apk", is_active=True,
        )
        self.assertEqual(publish_android_release(), newer)
        self.get.assert_not_called()


class AndroidDownloadTests(TestCase):
    setUp = workspace_fixtures.MobileWorkspaceTests.setUp
    web = workspace_fixtures.MobileWorkspaceTests.web

    def test_dashboard_button_redirects_to_selected_release(self):
        release = CallAppRelease.objects.create(
            version_name="1.6 Preview", version_code=10, is_active=True,
            download_url=f"https://shvya-ai.com/static/{APK_PATH}",
            sha256=APK_SHA256, release_notes="Installs alongside the previous app.",
        )
        client = self.web()
        page = client.get("/dashboard/call-intelligence/")
        self.assertContains(page, "1.6 Preview")
        self.assertContains(page, "Installs alongside the previous app.")
        self.assertContains(page, "/dashboard/call-intelligence/download/android/")
        self.assertRedirects(
            client.get("/dashboard/call-intelligence/download/android/"),
            release.download_url, fetch_redirect_response=False,
        )
