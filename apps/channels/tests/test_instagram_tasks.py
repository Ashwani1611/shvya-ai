from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.instagram_models import InstagramAccount, InstagramOAuthAttempt
from apps.channels.instagram_tasks import complete_instagram_oauth_task
from apps.organizations.models import Organization
from services.channels.instagram_service import InstagramAPIError


class InstagramTaskTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Instagram Task Org")
        self.user = User.objects.create_user(
            email="instagram-task@example.com",
            password="test-password",
            name="Instagram Task Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.attempt = InstagramOAuthAttempt.objects.create(
            organization=self.organization,
            created_by=self.user,
            authorization_code="",
            redirect_uri="https://dashboard.shvya-ai.com/dashboard/instagram/connect/return/",
            status=InstagramOAuthAttempt.Status.CONNECTED,
            completed_at=timezone.now(),
        )
        self.account = InstagramAccount.objects.create(
            organization=self.organization,
            ig_user_id="ig-task-business",
            username="task_business",
            access_token="encrypted-by-field",
            status=InstagramAccount.Status.CONNECTED,
            connected_by=self.user,
            connected_at=timezone.now(),
        )

    @patch("services.channels.instagram_service.sync_account_conversations")
    @patch("services.channels.instagram_service.subscribe_account_webhooks")
    @patch("services.channels.instagram_service.complete_oauth_attempt")
    def test_post_oauth_setup_failure_keeps_connected_state(
        self,
        complete_oauth_attempt,
        subscribe_account_webhooks,
        sync_account_conversations,
    ):
        complete_oauth_attempt.return_value = self.account
        subscribe_account_webhooks.side_effect = InstagramAPIError(
            "Meta webhook subscription needs attention.",
            status_code=400,
        )

        result = complete_instagram_oauth_task.run(str(self.attempt.id))

        self.account.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(result["status"], "connected_with_warning")
        self.assertEqual(self.account.status, InstagramAccount.Status.CONNECTED)
        self.assertIn("webhook subscription", self.account.last_error)
        self.assertEqual(self.attempt.status, InstagramOAuthAttempt.Status.CONNECTED)
        sync_account_conversations.assert_not_called()
