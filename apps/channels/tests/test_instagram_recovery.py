"""Durable recovery and stale-token persistence regressions."""
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.test import TestCase
from django.utils import timezone

from apps.channels.instagram_models import (
    InstagramAccount, InstagramConversation, InstagramMessage, InstagramWebhookDelivery,
)
from apps.channels.instagram_tasks import (
    _record_account_failure, process_instagram_webhook_delivery_task,
    recover_instagram_webhook_deliveries_task,
)
from apps.organizations.models import Organization
from services.channels import instagram_service as provider


class InstagramWebhookRecoveryTests(TestCase):
    def delivery(self, status, *, age=600, dispatch_age=None):
        row = InstagramWebhookDelivery.objects.create(
            payload_sha256=uuid.uuid4().hex, raw_payload={"object": "instagram", "entry": []},
            status=status,
            dispatched_at=timezone.now() - timedelta(seconds=dispatch_age) if dispatch_age is not None else None,
        )
        InstagramWebhookDelivery.objects.filter(pk=row.pk).update(
            received_at=timezone.now() - timedelta(seconds=age),
        )
        return row

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_recovery_requeues_lost_and_legacy_work_once(self, delay):
        rows = [self.delivery("pending"), self.delivery("processing"),
                self.delivery("processing", dispatch_age=600)]
        self.assertEqual(recover_instagram_webhook_deliveries_task.run(), {"recovered": 3, "failed": 0})
        self.assertEqual(delay.call_count, 3)
        self.assertEqual(recover_instagram_webhook_deliveries_task.run(), {"recovered": 0, "failed": 0})
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, "processing")
            self.assertGreater(row.dispatched_at, timezone.now() - timedelta(seconds=5))

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_fresh_and_terminal_envelopes_are_not_republished(self, delay):
        for status in ("failed", "processed", "ignored"):
            self.delivery(status)
        self.delivery("pending", age=10)
        self.delivery("processing", age=600, dispatch_age=10)
        self.delivery("processing", age=10)
        self.assertEqual(recover_instagram_webhook_deliveries_task.run()["recovered"], 0)
        delay.assert_not_called()

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_broker_failure_keeps_durable_work_eligible_for_recovery(self, delay):
        row = self.delivery("pending")
        delay.side_effect = RuntimeError("broker unavailable")
        self.assertEqual(recover_instagram_webhook_deliveries_task.run(), {"recovered": 0, "failed": 1})
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertIsNone(row.dispatched_at)
        delay.side_effect = None
        self.assertEqual(recover_instagram_webhook_deliveries_task.run()["recovered"], 1)

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_recovery_handles_completion_between_scan_and_dispatch(self, delay):
        from apps.channels.instagram_webhook import dispatch_instagram_delivery
        row = self.delivery("failed")
        self.assertFalse(dispatch_instagram_delivery(row.pk, recovery_only=True))
        delay.assert_not_called()

    def test_redelivered_worker_remains_idempotent(self):
        row = self.delivery("processing", dispatch_age=600)
        first = process_instagram_webhook_delivery_task.run(str(row.pk))
        second = process_instagram_webhook_delivery_task.run(str(row.pk))
        self.assertEqual(first["events"], 0)
        self.assertEqual(second["events"], 0)
        row.refresh_from_db()
        self.assertEqual(row.status, "ignored")
        self.assertTrue(process_instagram_webhook_delivery_task.acks_late)
        self.assertTrue(process_instagram_webhook_delivery_task.reject_on_worker_lost)

    def test_late_failed_worker_cannot_overwrite_successful_delivery(self):
        for status in ("processed", "ignored"):
            with self.subTest(status=status):
                row = self.delivery(status)
                provider.fail_webhook_delivery(row.pk, RuntimeError("older worker failure"))
                row.refresh_from_db()
                self.assertEqual(row.status, status)
                self.assertEqual(row.error_message, "")


class InstagramStaleSetupTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Instagram recovery")
        self.account = InstagramAccount.objects.create(
            organization=self.organization, ig_user_id="17841400000001999",
            access_token="old-test-token", status="connected", connected_at=timezone.now(),
        )

    def reconnect(self):
        current = InstagramAccount.objects.get(pk=self.account.pk)
        current.access_token = "new-test-token"
        current.connected_at = timezone.now()
        current.webhook_subscribed = False
        current.last_sync_at = None
        current.save()
        return current

    def test_stale_error_cannot_revoke_new_credentials(self):
        self.reconnect()
        self.assertFalse(_record_account_failure(self.account, provider.InstagramAPIError("Expired old token", code=190)))
        current = InstagramAccount.objects.get(pk=self.account.pk)
        self.assertEqual(current.status, "connected")
        self.assertEqual(current.last_error, "")
        self.assertEqual(current.access_token, "new-test-token")

    def test_current_error_is_recorded(self):
        self.assertTrue(_record_account_failure(self.account, provider.InstagramAPIError("Expired token", code=190)))
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "expired")

    @patch("services.channels.instagram_service._graph_post")
    def test_stale_subscription_does_not_call_meta(self, post):
        self.reconnect()
        with self.assertRaises(provider.InstagramOAuthSuperseded):
            provider.subscribe_account_webhooks(self.account)
        post.assert_not_called()

    @patch("services.channels.instagram_service._graph_post", return_value={"success": True})
    def test_subscription_requests_read_receipts_with_existing_messaging_scopes(self, post):
        provider.subscribe_account_webhooks(self.account)
        self.assertIn("messaging_seen", post.call_args.kwargs["params"]["subscribed_fields"].split(","))
        self.account.refresh_from_db()
        self.assertIn("messaging_seen", self.account.subscribed_fields)

    @patch("services.channels.instagram_service._graph_get")
    def test_sync_finishing_after_reconnect_does_not_mark_new_account_ready(self, get):
        def reconnect_while_syncing(*args, **kwargs):
            self.reconnect()
            return {"data": []}
        get.side_effect = reconnect_while_syncing
        with self.assertRaises(provider.InstagramOAuthSuperseded):
            provider.sync_account_conversations(self.account)
        current = InstagramAccount.objects.get(pk=self.account.pk)
        self.assertIsNone(current.last_sync_at)
        self.assertFalse(current.webhook_subscribed)

    @patch("services.channels.instagram_service._graph_get", return_value={})
    def test_malformed_sync_response_is_not_treated_as_empty_success(self, get):
        with self.assertRaises(provider.InstagramAPIError):
            provider.sync_account_conversations(self.account)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.last_sync_at)

    @patch("services.channels.instagram_service._graph_post", return_value={"message_id": "foreign-message"})
    def test_foreign_message_id_collision_never_deletes_or_returns_own_row(self, post):
        conversation = InstagramConversation.objects.create(
            organization=self.organization, account=self.account, participant_id="customer",
        )
        own = InstagramMessage.objects.create(
            organization=self.organization, account=self.account, conversation=conversation,
            direction="outbound", status="queued", sender_id=self.account.ig_user_id,
            recipient_id="customer", body="Reply",
        )
        other_org = Organization.objects.create(name="Other Instagram recovery")
        other_account = InstagramAccount.objects.create(organization=other_org, ig_user_id="other-owner")
        other_conversation = InstagramConversation.objects.create(
            organization=other_org, account=other_account, participant_id="other-customer",
        )
        foreign = InstagramMessage.objects.create(
            organization=other_org, account=other_account, conversation=other_conversation,
            direction="outbound", status="sent", external_id="foreign-message", body="Private",
        )
        with self.assertRaises(provider.InstagramAPIError):
            provider.send_queued_message(own)
        self.assertTrue(InstagramMessage.objects.filter(pk=own.pk).exists())
        foreign.refresh_from_db()
        self.assertEqual(foreign.body, "Private")
