"""Stale OAuth jobs and token refreshes cannot replace a newer connection."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.instagram_models import InstagramAccount, InstagramOAuthAttempt
from apps.channels.instagram_tasks import refresh_instagram_tokens_task
from apps.organizations.models import Organization
from services.channels import instagram_service as service


class InstagramOAuthLifecycleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Instagram lifecycle workspace")
        self.user = User.objects.create_user(
            email="instagram-lifecycle@example.com", password="test-password",
            organization=self.org, role=User.Role.ADMIN,
        )
        now = timezone.now()
        self.account = InstagramAccount.objects.create(
            organization=self.org, ig_user_id="17840000000001", access_token="original-token",
            status=InstagramAccount.Status.CONNECTED,
            connected_at=now - timedelta(days=2),
            token_refreshed_at=now - timedelta(days=2),
            token_expires_at=now + timedelta(days=2),
        )

    def attempt(self, *, completed=False):
        return InstagramOAuthAttempt.objects.create(
            organization=self.org, created_by=self.user,
            authorization_code="" if completed else "single-use-code",
            redirect_uri="https://dashboard.shvya-ai.com/dashboard/instagram/connect/return/",
            status=InstagramOAuthAttempt.Status.CONNECTED if completed else InstagramOAuthAttempt.Status.QUEUED,
            completed_at=self.account.connected_at if completed else None,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    @patch("services.channels.instagram_service._request")
    def test_completed_current_attempt_retry_does_not_exchange_code_again(self, request):
        attempt = self.attempt(completed=True)
        account = service.complete_oauth_attempt(attempt)
        self.assertEqual(account.pk, self.account.pk)
        request.assert_not_called()

    @patch("services.channels.instagram_service._request")
    def test_completed_attempt_after_disconnect_is_superseded_and_history_stays_successful(self, request):
        attempt = self.attempt(completed=True)
        service.disconnect(self.org)
        with self.assertRaises(service.InstagramOAuthSuperseded) as raised:
            service.complete_oauth_attempt(attempt)
        service.fail_oauth_attempt(attempt.pk, raised.exception)
        attempt.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(attempt.status, InstagramOAuthAttempt.Status.CONNECTED)
        self.assertEqual(self.account.status, InstagramAccount.Status.DISCONNECTED)
        request.assert_not_called()

    @patch("services.channels.instagram_service._request")
    def test_completed_attempt_cannot_reuse_later_connection_to_same_account(self, request):
        attempt = self.attempt(completed=True)
        InstagramAccount.objects.filter(pk=self.account.pk).update(
            access_token="new-token", connected_at=timezone.now(),
        )
        with self.assertRaises(service.InstagramOAuthSuperseded):
            service.complete_oauth_attempt(attempt)
        self.account.refresh_from_db()
        self.assertEqual(self.account.access_token, "new-token")
        request.assert_not_called()

    @patch("services.channels.instagram_service._request")
    def test_older_queued_attempt_never_exchanges_after_newer_request(self, request):
        old = self.attempt()
        self.attempt()
        with self.assertRaises(service.InstagramOAuthSuperseded) as raised:
            service.complete_oauth_attempt(old)
        service.fail_oauth_attempt(old.pk, raised.exception)
        old.refresh_from_db()
        self.assertEqual(old.status, InstagramOAuthAttempt.Status.FAILED)
        self.assertEqual(old.authorization_code, "")
        request.assert_not_called()

    @patch("services.channels.instagram_service._fetch_profile")
    @patch("services.channels.instagram_service._exchange_authorization_code")
    def test_new_attempt_arriving_during_exchange_prevents_old_credentials_persisting(self, exchange, profile):
        old = self.attempt()

        def exchange_and_queue_newer(**kwargs):
            self.attempt()
            return "older-authorization-token", 5_184_000, {"user_id": self.account.ig_user_id}

        exchange.side_effect = exchange_and_queue_newer
        profile.return_value = {"resolved_ig_user_id": self.account.ig_user_id, "user_id": self.account.ig_user_id}
        with self.assertRaises(service.InstagramOAuthSuperseded):
            service.complete_oauth_attempt(old)
        self.account.refresh_from_db()
        self.assertEqual(self.account.access_token, "original-token")

    @patch("services.channels.instagram_service._request")
    def test_failed_authorization_cannot_be_run_directly(self, request):
        attempt = self.attempt()
        InstagramOAuthAttempt.objects.filter(pk=attempt.pk).update(status=InstagramOAuthAttempt.Status.FAILED)
        with self.assertRaises(service.InstagramOAuthSuperseded):
            service.complete_oauth_attempt(attempt)
        request.assert_not_called()

    @patch("services.channels.instagram_service._request")
    def test_stale_refresh_snapshot_does_not_revive_disconnected_account(self, request):
        service.disconnect(self.org)
        self.assertIsNone(service.refresh_account_token(self.account))
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, InstagramAccount.Status.DISCONNECTED)
        self.assertEqual(self.account.access_token, "")
        request.assert_not_called()

    @patch("services.channels.instagram_service._request")
    def test_stale_refresh_snapshot_respects_new_authorization_token_age(self, request):
        InstagramAccount.objects.filter(pk=self.account.pk).update(
            access_token="new-token", connected_at=timezone.now(), token_refreshed_at=timezone.now(),
        )
        self.assertIsNone(service.refresh_account_token(self.account))
        self.account.refresh_from_db()
        self.assertEqual(self.account.access_token, "new-token")
        request.assert_not_called()

    @patch("services.channels.instagram_service._request")
    def test_expired_token_is_never_sent_to_refresh_endpoint(self, request):
        InstagramAccount.objects.filter(pk=self.account.pk).update(token_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(service.InstagramAPIError) as raised:
            service.refresh_account_token(self.account)
        self.assertTrue(raised.exception.token_invalid)
        request.assert_not_called()

    @patch("services.channels.instagram_service._request")
    def test_connection_time_prevents_refreshing_young_legacy_token(self, request):
        InstagramAccount.objects.filter(pk=self.account.pk).update(
            connected_at=timezone.now(), token_refreshed_at=None,
        )
        self.assertIsNone(service.refresh_account_token(self.account))
        request.assert_not_called()

    def test_refresh_candidates_must_be_unexpired_and_at_least_24_hours_old(self):
        self.assertEqual(list(service.accounts_due_for_token_refresh()), [self.account])
        InstagramAccount.objects.filter(pk=self.account.pk).update(token_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(service.accounts_due_for_token_refresh().exists())
        InstagramAccount.objects.filter(pk=self.account.pk).update(
            token_expires_at=timezone.now() + timedelta(days=2), token_refreshed_at=timezone.now(),
        )
        self.assertFalse(service.accounts_due_for_token_refresh().exists())
        InstagramAccount.objects.filter(pk=self.account.pk).update(token_refreshed_at=None, connected_at=timezone.now())
        self.assertFalse(service.accounts_due_for_token_refresh().exists())

    @patch("services.channels.instagram_service.refresh_account_token", return_value=None)
    def test_refresh_task_reports_skips_separately(self, refresh):
        result = refresh_instagram_tokens_task.run()
        self.assertEqual(result, {"refreshed": 0, "failed": 0, "skipped": 1})
        refresh.assert_called_once()

    @patch("services.channels.instagram_service.refresh_account_token")
    def test_failed_old_refresh_does_not_revoke_newer_connection(self, refresh):
        def reconnect_before_old_error(account):
            InstagramAccount.objects.filter(pk=account.pk).update(
                access_token="new-token", connected_at=timezone.now(), token_refreshed_at=timezone.now(),
            )
            raise service.InstagramAPIError("Old token expired", code=190)

        refresh.side_effect = reconnect_before_old_error
        result = refresh_instagram_tokens_task.run()
        self.account.refresh_from_db()
        self.assertEqual(result, {"refreshed": 0, "failed": 0, "skipped": 1})
        self.assertEqual(self.account.status, InstagramAccount.Status.CONNECTED)
        self.assertEqual(self.account.access_token, "new-token")
        self.assertEqual(self.account.last_error, "")
