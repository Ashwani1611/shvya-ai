from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppClient
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_api_runtime import (
    _contact_names_from_payload,
    _send_task_postrun,
    normalize_meta_recipient,
)


class WhatsAppAPITransportRuntimeTests(SimpleTestCase):
    def test_meta_recipient_strips_e164_plus_and_formatting(self):
        self.assertEqual(normalize_meta_recipient("+91 94702-25755"), "919470225755")
        self.assertEqual(normalize_meta_recipient("919470225755"), "919470225755")

    def test_contact_profile_name_is_extracted_from_meta_webhook(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [
                                    {
                                        "wa_id": "919470225755",
                                        "profile": {"name": "Gaurav Sharma"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        self.assertEqual(
            _contact_names_from_payload(payload),
            {"919470225755": "Gaurav Sharma"},
        )

    def test_installed_provider_sends_digits_to_meta(self):
        client = WhatsAppClient(phone_number_id="phone-id", access_token="token")
        with patch.object(client, "_post", return_value={"messages": [{"id": "wamid.1"}]}) as post:
            client.send_text_message(to="+919470225755", body="Hello")
        payload = post.call_args.args[1]
        self.assertEqual(payload["to"], "919470225755")


class WhatsAppAPITerminalStatusTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Delivery Org")
        self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales")
        self.stage = Stage.objects.create(pipeline=self.pipeline, name="New leads")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="API",
            phone_number_id="123456789",
            waba_id="987654321",
            access_token="token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="WhatsApp Lead",
            phone="+919470225755",
        )

    @patch("services.channels.realtime.publish_status")
    def test_permanent_sender_failure_cannot_remain_stuck_as_queued(self, publish_status):
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.phone_number_id,
            to_number=self.lead.phone,
            body="Hello",
            status=WhatsAppMessage.Status.QUEUED,
        )

        _send_task_postrun(
            task=SimpleNamespace(name="apps.channels.tasks.send_whatsapp_message_task"),
            args=[str(message.id)],
            retval={"status": "failed", "error": "Meta rejected the message"},
        )

        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertIn("Meta rejected", message.error)
        publish_status.assert_called_once()
