from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization
from services.channels.whatsapp_api_runtime import (
    _bind_outbound_to_source_api_account,
    _contact_names_from_payload,
    _meta_api_error_text,
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

    def test_meta_400_keeps_actionable_error_details(self):
        error = WhatsAppAPIError(
            "WhatsApp API returned 400",
            status_code=400,
            response_body=(
                '{"error":{"message":"Re-engagement message outside allowed window",'
                '"type":"OAuthException","code":131047,'
                '"error_data":{"details":"Customer service window expired"},'
                '"fbtrace_id":"trace-123"}}'
            ),
        )
        text = _meta_api_error_text(error)
        self.assertIn("WhatsApp API returned 400", text)
        self.assertIn("code 131047", text)
        self.assertIn("Customer service window expired", text)
        self.assertIn("fbtrace_id trace-123", text)


class WhatsAppAPITerminalStatusTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Delivery Org")
        self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales")
        self.stage = self.pipeline.stages.get(name="New leads")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
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

    @patch("services.channels.realtime.publish_status")
    def test_terminal_status_replaces_generic_error_with_meta_reason(self, publish_status):
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.phone_number_id,
            to_number=self.lead.phone,
            body="Hello",
            status=WhatsAppMessage.Status.FAILED,
            error="WhatsApp API returned 400",
        )

        _send_task_postrun(
            task=SimpleNamespace(name="apps.channels.tasks.send_whatsapp_message_task"),
            args=[str(message.id)],
            retval={
                "status": "failed",
                "error": "WhatsApp API returned 400; code 131047; Customer service window expired",
            },
        )

        message.refresh_from_db()
        self.assertIn("code 131047", message.error)
        publish_status.assert_called_once()

    def test_ai_reply_uses_exact_api_account_that_received_source_inbound(self):
        other_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Wrong API",
            phone_number_id="999999999",
            waba_id="other-waba",
            access_token="other-token",
            display_phone_number="+15550000002",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        inbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wamid.inbound",
            from_number="919470225755",
            to_number="+15550000001",
            body="Hi",
            status=WhatsAppMessage.Status.RECEIVED,
        )
        outbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=other_account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=other_account.phone_number_id,
            to_number=self.lead.phone,
            body="Hi! Thanks for reaching out.",
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={
                "shvya_ai": {
                    "source_inbound_message_id": str(inbound.id),
                }
            },
        )

        target = _bind_outbound_to_source_api_account(outbound)

        outbound.refresh_from_db()
        self.assertIsNotNone(target)
        self.assertEqual(target.id, self.account.id)
        self.assertEqual(outbound.account_id, self.account.id)
        self.assertEqual(outbound.from_number, self.account.phone_number_id)
