from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.crm.models import Lead, Pipeline, Stage
from apps.integrations.models import WebhookConfiguration, WebhookDelivery
from apps.integrations.services.webhook import (
    WEBHOOK_DELIVERY_HEADER,
    WEBHOOK_SECRET_HEADER,
    validate_webhook_url,
)
from apps.integrations.tasks import deliver_webhook_task
from apps.organizations.models import Organization


class WebhookConfigurationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Webhook Test Org")

    def test_secret_is_encrypted_and_can_be_recovered(self):
        webhook = WebhookConfiguration.objects.create(
            organization=self.organization,
            endpoint_url="https://example.com/webhook",
        )
        webhook.set_secret("super-secret-value")
        webhook.save()

        self.assertNotEqual(webhook.encrypted_secret, "super-secret-value")
        self.assertTrue(webhook.has_secret)
        self.assertEqual(webhook.get_secret(), "super-secret-value")

    def test_webhook_url_requires_https_and_rejects_localhost(self):
        self.assertEqual(
            validate_webhook_url("https://example.com/webhook"),
            "https://example.com/webhook",
        )

        with self.assertRaises(ValidationError):
            validate_webhook_url("http://example.com/webhook")

        with self.assertRaises(ValidationError):
            validate_webhook_url("https://localhost/webhook")

        with self.assertRaises(ValidationError):
            validate_webhook_url("https://127.0.0.1/webhook")


class LeadWebhookSignalTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Signal Test Org")
        self.pipeline = Pipeline.objects.filter(
            organization=self.organization,
            name="Leads",
        ).first()
        self.stage = Stage.objects.filter(
            pipeline=self.pipeline,
        ).order_by("display_order").first()

        self.webhook = WebhookConfiguration.objects.create(
            organization=self.organization,
            endpoint_url="https://example.com/webhook",
            is_enabled=True,
        )
        self.webhook.set_secret("signal-secret")
        self.webhook.save()

    def test_create_and_update_generate_delivery_payloads(self):
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="John Doe",
            phone="+919123456789",
            email="john@example.com",
            notes="Interested",
            attributes={"company": "Example Pvt Ltd"},
        )

        create_delivery = WebhookDelivery.objects.get(
            lead_id=lead.id,
            event_type=WebhookDelivery.EventType.CREATE,
        )
        self.assertEqual(create_delivery.payload["name"], "John Doe")
        self.assertEqual(create_delivery.payload["pipeline"], self.pipeline.name)
        self.assertEqual(create_delivery.payload["stage"], self.stage.name)
        self.assertEqual(create_delivery.payload["event_type"], "create")
        self.assertEqual(
            create_delivery.payload["custom_attributes"],
            {"company": "Example Pvt Ltd"},
        )

        lead.notes = "Updated notes"
        lead.save(update_fields=["notes", "updated_at"])

        update_delivery = WebhookDelivery.objects.get(
            lead_id=lead.id,
            event_type=WebhookDelivery.EventType.UPDATE,
        )
        self.assertEqual(update_delivery.payload["notes"], "Updated notes")
        self.assertEqual(update_delivery.payload["event_type"], "update")


class WebhookDeliveryTaskTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Task Test Org")
        self.webhook = WebhookConfiguration.objects.create(
            organization=self.organization,
            endpoint_url="https://example.com/webhook",
            is_enabled=True,
        )
        self.webhook.set_secret("task-secret")
        self.webhook.save()
        self.delivery = WebhookDelivery.objects.create(
            webhook=self.webhook,
            organization=self.organization,
            lead_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            event_type=WebhookDelivery.EventType.CREATE,
            payload={"lead_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        )

    @patch("apps.integrations.tasks.assert_public_webhook_target")
    @patch("apps.integrations.tasks.requests.post")
    def test_successful_delivery_sends_headers_and_marks_sent(
        self,
        mock_post,
        mock_public_target,
    ):
        mock_public_target.return_value = "https://example.com/webhook"
        mock_post.return_value = Mock(status_code=200, text="ok")

        result = deliver_webhook_task.run(str(self.delivery.id))

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.SENT)
        self.assertEqual(self.delivery.attempt_count, 1)
        self.assertEqual(self.delivery.response_status, 200)
        self.assertIsNotNone(self.delivery.delivered_at)
        self.assertEqual(result["status"], "sent")

        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["json"], self.delivery.payload)
        self.assertEqual(
            call_kwargs["headers"][WEBHOOK_SECRET_HEADER],
            "task-secret",
        )
        self.assertEqual(
            call_kwargs["headers"][WEBHOOK_DELIVERY_HEADER],
            str(self.delivery.id),
        )
        self.assertFalse(call_kwargs["allow_redirects"])
