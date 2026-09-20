from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.crm.models import Lead, Pipeline, Stage
from apps.integrations.models import WebhookConfiguration, WebhookDelivery
from apps.integrations.services.webhook import (
    WEBHOOK_DELIVERY_HEADER,
    WEBHOOK_SECRET_HEADER,
    _PinnedHTTPSConnection,
    assert_public_webhook_target,
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

    @patch("apps.integrations.services.webhook.socket.getaddrinfo")
    def test_public_target_is_pinned_to_resolved_ip(self, getaddrinfo):
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]

        target = assert_public_webhook_target(
            "https://example.com/hooks/lead?source=shvya"
        )

        self.assertEqual(target.hostname, "example.com")
        self.assertEqual(target.connect_ip, "93.184.216.34")
        self.assertEqual(target.request_target, "/hooks/lead?source=shvya")

    @patch("apps.integrations.services.webhook.socket.create_connection")
    @patch("apps.integrations.services.webhook.ssl.create_default_context")
    def test_pinned_https_socket_uses_validated_ip_and_original_sni(
        self,
        default_context,
        create_connection,
    ):
        raw_socket = Mock()
        wrapped_socket = Mock()
        create_connection.return_value = raw_socket
        context = Mock()
        context.wrap_socket.return_value = wrapped_socket
        default_context.return_value = context

        connection = _PinnedHTTPSConnection(
            hostname="example.com",
            connect_ip="93.184.216.34",
            port=443,
            timeout=10,
        )
        connection.connect()

        create_connection.assert_called_once_with(
            ("93.184.216.34", 443),
            10,
            None,
        )
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="example.com",
        )
        self.assertIs(connection.sock, wrapped_socket)



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

    @patch("apps.integrations.tasks.post_webhook_json")
    @patch("apps.integrations.tasks.assert_public_webhook_target")
    def test_successful_delivery_sends_headers_and_marks_sent(
        self,
        mock_public_target,
        mock_post,
    ):
        target = Mock()
        mock_public_target.return_value = target
        mock_post.return_value = (200, "ok")

        result = deliver_webhook_task.run(str(self.delivery.id))

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.SENT)
        self.assertEqual(self.delivery.attempt_count, 1)
        self.assertEqual(self.delivery.response_status, 200)
        self.assertIsNotNone(self.delivery.delivered_at)
        self.assertEqual(result["status"], "sent")

        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["payload"], self.delivery.payload)
        self.assertEqual(
            call_kwargs["headers"][WEBHOOK_SECRET_HEADER],
            "task-secret",
        )
        self.assertEqual(
            call_kwargs["headers"][WEBHOOK_DELIVERY_HEADER],
            str(self.delivery.id),
        )
        self.assertIs(mock_post.call_args.args[0], target)
