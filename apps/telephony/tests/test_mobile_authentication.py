"""Exercise the Android auth contract with real JWTs and the full middleware."""

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.telephony.models import CallDevice, CallRecord
from . import test_call_intelligence as fixtures


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class MobileAuthenticationTests(TestCase):
    setUp = fixtures.CallIntelligenceTests.setUp
    payload = fixtures.CallIntelligenceTests.payload

    def login(self):
        # Do not force_authenticate: these tests cover the exact token transport
        # used by the APK, without any web-login session cookie.
        cache.clear()
        client = APIClient()
        response = client.post(
            "/api/v1/auth/token/",
            {"email": self.user.email, "password": "secret123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        return client, response.data

    def assert_workspace_access(self, client):
        for path in ("today/?mine=1", "calls/?mine=1", "reminders/", "settings/"):
            with self.subTest(path=path):
                response = client.get(f"/api/v1/call-intelligence/{path}")
                self.assertEqual(response.status_code, 200, response.data)

    def test_login_sync_reminders_and_refresh_keep_same_access_token_valid(self):
        client, tokens = self.login()
        response = client.post(
            "/api/v1/call-intelligence/devices/register/",
            {"device_id": "android-001", "name": "Sales phone"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        response = client.post(
            "/api/v1/call-intelligence/devices/heartbeat/",
            {"device_id": "android-001", "permissions": {"call_log": True}},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        response = client.post(
            "/api/v1/call-intelligence/events/", self.payload(), format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assert_workspace_access(client)
        self.assertEqual(str(AccessToken(tokens["access"])["user_id"]), str(self.user.pk))
        self.assertFalse(client.cookies)

    def test_expired_access_can_refresh_and_rotated_token_is_usable(self):
        client, tokens = self.login()
        expired = AccessToken(tokens["access"])
        expired.set_exp(lifetime=timedelta(seconds=-1))
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {expired}")
        self.assertEqual(client.get("/api/v1/call-intelligence/reminders/").status_code, 401)

        anonymous_client = APIClient()
        response = anonymous_client.post(
            "/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        rotated = response.data
        self.assertNotEqual(rotated["refresh"], tokens["refresh"])
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {rotated['access']}")
        self.assert_workspace_access(client)

        # Rotation must remain secure: the old refresh token cannot be replayed.
        response = anonymous_client.post(
            "/api/v1/auth/token/refresh/", {"refresh": tokens["refresh"]}, format="json",
        )
        self.assertEqual(response.status_code, 401)
        response = anonymous_client.post(
            "/api/v1/auth/token/refresh/", {"refresh": rotated["refresh"]}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assert_workspace_access(client)

    def test_removed_device_stops_tracking_without_revoking_workspace_login(self):
        client, tokens = self.login()
        device = CallDevice.objects.get(device_id="android-001")
        previous_seen = device.last_seen_at
        device.is_active = False
        device.save(update_fields=["is_active", "updated_at"])
        for path, payload in (
            ("devices/register/", {"device_id": device.device_id}),
            ("devices/heartbeat/", {"device_id": device.device_id}),
            ("events/", self.payload()),
        ):
            with self.subTest(path=path):
                response = client.post(
                    f"/api/v1/call-intelligence/{path}", payload, format="json",
                )
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["code"], "device_removed")
        device.refresh_from_db()
        self.assertFalse(device.is_active)
        self.assertEqual(device.last_seen_at, previous_seen)
        self.assertEqual(CallRecord.objects.count(), 0)
        self.assert_workspace_access(client)
        self.assertEqual(str(RefreshToken(tokens["refresh"])["user_id"]), str(self.user.pk))

    def test_unknown_or_another_users_device_is_not_disclosed_by_heartbeat(self):
        client, _ = self.login()
        colleague = type(self.user).objects.create_user(
            email="colleague@example.com", organization=self.org, password="secret123",
        )
        CallDevice.objects.create(
            organization=self.org, user=colleague,
            device_id="colleague-phone", is_active=False,
        )
        CallDevice.objects.create(
            organization=self.other_org, user=self.other_user,
            device_id="another-organization-phone", is_active=False,
        )
        for device_id in ("unknown-phone", "colleague-phone", "another-organization-phone"):
            with self.subTest(device_id=device_id):
                response = client.post(
                    "/api/v1/call-intelligence/devices/heartbeat/",
                    {"device_id": device_id}, format="json",
                )
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("code", response.data)

    def test_module_denial_is_not_an_expired_login(self):
        client, _ = self.login()
        self.org.package = "diy"
        self.org.save(update_fields=["package", "updated_at"])
        for path in ("today/", "reminders/"):
            response = client.get(f"/api/v1/call-intelligence/{path}")
            self.assertEqual(response.status_code, 403)
        self.org.package = "enterprise"
        self.org.save(update_fields=["package", "updated_at"])
        self.assert_workspace_access(client)

    def test_deactivated_organization_still_cannot_access_mobile_workspace(self):
        client, _ = self.login()
        self.org.is_active = False
        self.org.save(update_fields=["is_active", "updated_at"])
        self.assertEqual(client.get("/api/v1/call-intelligence/reminders/").status_code, 403)
        self.assertEqual(client.post(
            "/api/v1/call-intelligence/events/", self.payload(), format="json",
        ).status_code, 403)
        self.assertEqual(CallRecord.objects.count(), 0)
