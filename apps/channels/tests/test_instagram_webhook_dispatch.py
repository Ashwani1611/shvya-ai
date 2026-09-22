"""Real-transaction regressions for webhook dispatch and broker recovery."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from threading import Barrier
from unittest.mock import patch

from django.db import connections, transaction
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.utils import timezone

from apps.channels.instagram_models import InstagramWebhookDelivery
from apps.channels.instagram_webhook import instagram_webhook_view
from services.channels.instagram_service import process_webhook_delivery


@override_settings(META_INSTAGRAM_APP_SECRET="dispatch-test-secret")
class InstagramWebhookDispatchTests(TransactionTestCase):
    def setUp(self):
        self.body = json.dumps({"object": "instagram", "entry": []}).encode()
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.signature = "sha256=" + hmac.new(
            b"dispatch-test-secret", self.body, hashlib.sha256,
        ).hexdigest()

    def deliver(self):
        request = RequestFactory().post(
            "/webhooks/instagram/", self.body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=self.signature,
        )
        return instagram_webhook_view(request)

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_successful_dispatch_is_claimed_and_duplicate_does_not_publish(self, delay):
        self.assertEqual(self.deliver().status_code, 200)
        delivery = InstagramWebhookDelivery.objects.get(payload_sha256=self.digest)
        self.assertEqual(delivery.status, InstagramWebhookDelivery.Status.PROCESSING)
        self.assertEqual(self.deliver().status_code, 200)
        delay.assert_called_once_with(str(delivery.pk))
        self.assertEqual(InstagramWebhookDelivery.objects.count(), 1)

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_broker_failure_rolls_back_claim_not_durable_envelope(self, delay):
        delay.side_effect = RuntimeError("broker unavailable")
        response = self.deliver()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "5")
        delivery = InstagramWebhookDelivery.objects.get(payload_sha256=self.digest)
        self.assertEqual(delivery.status, InstagramWebhookDelivery.Status.PENDING)
        self.assertEqual(delivery.raw_payload, {"object": "instagram", "entry": []})
        delay.side_effect = None
        self.assertEqual(self.deliver().status_code, 200)
        self.assertEqual(self.deliver().status_code, 200)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, InstagramWebhookDelivery.Status.PROCESSING)
        self.assertEqual(delay.call_count, 2)
        self.assertEqual(InstagramWebhookDelivery.objects.count(), 1)

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_failed_processing_can_be_requeued_and_clears_error(self, delay):
        delivery = InstagramWebhookDelivery.objects.create(
            payload_sha256=self.digest, raw_payload={"object": "instagram", "entry": []},
            status=InstagramWebhookDelivery.Status.FAILED,
            error_message="previous processing error", processed_at=timezone.now(),
        )
        self.assertEqual(self.deliver().status_code, 200)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, InstagramWebhookDelivery.Status.PROCESSING)
        self.assertEqual(delivery.error_message, "")
        self.assertIsNone(delivery.processed_at)
        delay.assert_called_once_with(str(delivery.pk))

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_completed_envelopes_are_never_republished(self, delay):
        delivery = InstagramWebhookDelivery.objects.create(
            payload_sha256=self.digest, raw_payload={"object": "instagram", "entry": []},
        )
        for state in (
            InstagramWebhookDelivery.Status.PROCESSED,
            InstagramWebhookDelivery.Status.IGNORED,
            InstagramWebhookDelivery.Status.PROCESSING,
        ):
            with self.subTest(state=state):
                delivery.status = state
                delivery.save(update_fields=["status"])
                self.assertEqual(self.deliver().status_code, 200)
        delay.assert_not_called()

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_processor_accepts_dispatch_claim_and_remains_idempotent(self, delay):
        self.assertEqual(self.deliver().status_code, 200)
        delivery = InstagramWebhookDelivery.objects.get(payload_sha256=self.digest)
        with transaction.atomic():
            self.assertEqual(process_webhook_delivery(delivery), 0)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, InstagramWebhookDelivery.Status.IGNORED)
        self.assertEqual(self.deliver().status_code, 200)
        delay.assert_called_once_with(str(delivery.pk))

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_envelope_is_visible_to_another_connection_before_publish(self, delay):
        def read_envelope(delivery_id):
            try:
                return InstagramWebhookDelivery.objects.filter(pk=delivery_id).exists()
            finally:
                connections["default"].close()

        def publish(delivery_id):
            with ThreadPoolExecutor(max_workers=1) as executor:
                visible = executor.submit(read_envelope, delivery_id).result(timeout=10)
            self.assertTrue(visible)

        delay.side_effect = publish
        self.assertEqual(self.deliver().status_code, 200)
        delay.assert_called_once()

    @patch("apps.channels.instagram_webhook.process_instagram_webhook_delivery_task.delay")
    def test_concurrent_duplicate_deliveries_publish_once(self, delay):
        barrier = Barrier(2)

        def deliver_concurrently():
            try:
                barrier.wait(timeout=10)
                return self.deliver().status_code
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(deliver_concurrently) for _ in range(2)]
            codes = [future.result(timeout=20) for future in futures]
        self.assertEqual(codes, [200, 200])
        delivery = InstagramWebhookDelivery.objects.get(payload_sha256=self.digest)
        delay.assert_called_once_with(str(delivery.pk))
        self.assertEqual(InstagramWebhookDelivery.objects.count(), 1)
