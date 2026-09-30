"""Connection status must reflect fresh authorization and inbox setup."""
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.channels.instagram_models import InstagramAccount, InstagramOAuthAttempt
from apps.channels.instagram_ui import _connection_context
from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.organizations.models import Organization


class InstagramConnectionStatusTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Instagram status")
        self.account = InstagramAccount.objects.create(
            organization=self.organization,
            ig_user_id="17841400000000123",
            access_token="test-token",
            status=InstagramAccount.Status.CONNECTED,
            webhook_subscribed=True,
            last_sync_at=timezone.now(),
            token_expires_at=timezone.now() + timedelta(days=30),
        )

    def test_expired_token_requires_reconnect_even_before_refresh_worker_runs(self):
        self.account.token_expires_at = timezone.now() - timedelta(seconds=1)
        self.account.save(update_fields=["token_expires_at"])
        context = _connection_context(self.organization)
        self.assertFalse(context["instagram_connected"])
        self.assertFalse(context["instagram_inbox_ready"])
        self.assertEqual(context["instagram_connection_state"], "expired")

    def test_recorded_setup_failure_does_not_show_ready_from_old_markers(self):
        self.account.last_error = "Meta did not confirm the webhook subscription."
        self.account.save(update_fields=["last_error"])
        context = _connection_context(self.organization)
        self.assertTrue(context["instagram_connected"])
        self.assertFalse(context["instagram_inbox_ready"])
        self.assertEqual(context["instagram_connection_state"], "authorized_with_warning")

    def test_authorized_account_remains_pending_setup_until_both_checks_finish(self):
        self.account.webhook_subscribed = False
        self.account.last_sync_at = None
        self.account.save(update_fields=["webhook_subscribed", "last_sync_at"])
        context = _connection_context(self.organization)
        self.assertEqual(context["instagram_connection_state"], "authorized_with_warning")
        self.account.webhook_subscribed = True
        self.account.last_sync_at = timezone.now()
        self.account.save(update_fields=["webhook_subscribed", "last_sync_at"])
        self.assertEqual(_connection_context(self.organization)["instagram_connection_state"], "ready")

    def test_failed_exchange_is_an_explicit_failure_without_a_connected_account(self):
        self.account.delete()
        user = User.objects.create_user(
            email="instagram-status@example.com", password="test-password",
            name="Instagram Admin", organization=self.organization,
        )
        InstagramOAuthAttempt.objects.create(
            organization=self.organization, created_by=user,
            redirect_uri="https://example.com/return/",
            status=InstagramOAuthAttempt.Status.FAILED,
            error_message="Instagram login did not return an access token.",
        )
        context = _connection_context(self.organization)
        self.assertEqual(context["instagram_connection_state"], "failed")
        self.assertFalse(context["instagram_oauth_pending"])
        self.assertIn("access token", context["instagram_connection_error"])

    def test_failed_reconnect_does_not_hide_a_healthy_existing_inbox(self):
        user = User.objects.create_user(
            email="instagram-reconnect@example.com", password="test-password",
            name="Instagram Admin", organization=self.organization,
        )
        InstagramOAuthAttempt.objects.create(
            organization=self.organization, created_by=user,
            redirect_uri="https://example.com/return/",
            status=InstagramOAuthAttempt.Status.FAILED,
            error_message="The new authorization expired. Please connect again.",
        )
        context = _connection_context(self.organization)
        self.assertTrue(context["instagram_inbox_ready"])
        self.assertEqual(context["instagram_connection_state"], "ready")
        self.assertIn("authorization expired", context["instagram_connection_error"])


@override_settings(META_INSTAGRAM_APP_ID="ig-app", META_INSTAGRAM_APP_SECRET="ig-secret")
class InstagramOAuthCallbackIntegrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organization = Organization.objects.create(name="OAuth completion", package="dfy")
        self.user = User.objects.create_user(
            email="oauth-complete@example.com", password="test-password", name="Admin",
            organization=self.organization, role=User.Role.ADMIN,
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def start(self):
        response = self.client.get(reverse("crm-instagram-oauth-start"))
        self.assertIn("no-store", response["Cache-Control"])
        return parse_qs(urlsplit(response["Location"]).query)

    @patch("services.channels.instagram_service._request")
    def test_meta_wrapped_response_connects_then_subscribes_and_syncs(self, request):
        request.side_effect = [
            {"data": [{"access_token": "short-test-token", "user_id": "app-scoped-id",
                        "permissions": ["instagram_business_basic", "instagram_business_manage_messages"]}]},
            {"access_token": "long-test-token", "expires_in": 5184000},
            {"data": [{"id": "app-scoped-id", "user_id": "17841400000000456", "username": "shvya_test"}]},
            {"success": True},
            {"data": []},
        ]
        params = self.start()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.get(reverse("crm-instagram-oauth-return"), {
                "code": "one-time-test-code", "state": params["state"][0],
            })
        self.assertRedirects(response, reverse("crm-instagram-connect"))
        account = InstagramAccount.objects.get(organization=self.organization)
        self.assertEqual(account.ig_user_id, "17841400000000456")
        self.assertTrue(account.webhook_subscribed)
        self.assertIsNotNone(account.last_sync_at)
        attempt = InstagramOAuthAttempt.objects.get(organization=self.organization)
        self.assertEqual(attempt.status, "connected")
        self.assertEqual(attempt.authorization_code, "")
        exchange = request.call_args_list[0]
        self.assertEqual(exchange.kwargs["data"]["redirect_uri"], params["redirect_uri"][0])
        self.assertIn("17841400000000456/subscribed_apps", request.call_args_list[3].args[1])
        response = self.client.get(reverse("crm-instagram-connect"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["instagram_connection_state"], "ready")
        for secret in ("short-test-token", "long-test-token", "one-time-test-code", "ig-secret"):
            self.assertNotContains(response, secret)

    @patch("apps.channels.instagram_ui.complete_instagram_oauth_task.delay")
    def test_callback_replay_does_not_exchange_code_twice(self, delay):
        params = self.start()
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.get(reverse("crm-instagram-oauth-return"), {
                    "code": "one-time-test-code", "state": params["state"][0],
                })
        self.assertEqual(InstagramOAuthAttempt.objects.filter(organization=self.organization).count(), 1)
        delay.assert_called_once()

    @patch("apps.channels.instagram_ui.complete_instagram_oauth_task.delay")
    def test_permission_denial_is_visible_inline_without_queuing(self, delay):
        params = self.start()
        response = self.client.get(reverse("crm-instagram-oauth-return"), {
            "error": "access_denied", "state": params["state"][0],
        }, follow=True)
        delay.assert_not_called()
        self.assertContains(response, "Instagram authorization was cancelled or permissions were not granted.")
        self.assertContains(response, 'role="alert"')
