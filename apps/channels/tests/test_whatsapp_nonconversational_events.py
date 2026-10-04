"""Transport notices and empty text must not start customer qualification."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from apps.ai_engagement.services.engagement_execution import _whatsapp_send_eligible
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization
from services.channels.whatsapp_service import (
    extract_inbound_message_body, handle_inbound_message, inbound_message_supports_ai,
)


class WhatsAppInboundContentEligibilityTests(SimpleTestCase):
    def test_notifications_unknown_types_and_empty_text_do_not_authorize_ai(self):
        for payload, body in (
            ({"type": "system", "system": {"type": "customer_changed_number"}}, ""),
            ({"type": "unknown", "errors": [{"code": 131051}]}, ""),
            ({"type": "unsupported"}, "Unsupported message type"),
            ({"type": "notification"}, "Hello"),
            ({"type": "reaction", "reaction": {"emoji": "👍"}}, ""),
            ({"type": "text", "text": {"body": "   "}}, ""),
            ({"type": "text", "text": []}, ""),
            ({"type": "button", "button": {}}, ""),
            ({"type": "interactive", "interactive": {"type": "button_reply"}}, ""),
            ({}, ""),
        ):
            with self.subTest(payload=payload):
                self.assertFalse(inbound_message_supports_ai(body=body, raw_payload=payload))

    def test_supported_customer_text_buttons_lists_and_media_remain_eligible(self):
        payloads = [
            {"type": "text", "text": {"body": "What does your company do?"}},
            {"type": "button", "button": {"text": "Interested"}},
            {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"title": "Yes"}}},
            {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"title": "Enterprise"}}},
            *[{"type": kind, kind: {"id": "provider-media"}} for kind in ("image", "audio", "video", "document")],
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertTrue(inbound_message_supports_ai(body="", raw_payload=payload))
        self.assertTrue(inbound_message_supports_ai(body="Legacy customer text", raw_payload={}))
        self.assertTrue(inbound_message_supports_ai(body="", raw_payload={}, message_type="audio"))
        self.assertTrue(inbound_message_supports_ai(body="Hosted customer text", raw_payload={"messageType": "text"}))

    def test_media_captions_are_customer_text_without_invented_attachment_content(self):
        for kind in ("image", "video", "document"):
            with self.subTest(kind=kind):
                self.assertEqual(extract_inbound_message_body({
                    "type": kind, kind: {"id": "provider-media", "caption": "Please explain this quotation"},
                }), "Please explain this quotation")
                self.assertEqual(extract_inbound_message_body({"type": kind, kind: {"id": "provider-media"}}), "")

    def test_old_queued_notification_is_rejected_before_all_execution_wrappers(self):
        from apps.ai_engagement.tasks import _execute_ai_engagement_response

        for payload in ({"type": "unknown"}, {"type": "text", "text": {"body": ""}}):
            with self.subTest(payload=payload):
                source = SimpleNamespace(pk="source-id", body="", raw_payload=payload, message_type="text")
                with (
                    patch.object(WhatsAppMessage.objects, "filter") as messages,
                    patch("apps.ai_engagement.services.execution_tracker.record_execution") as record,
                    patch("apps.ai_engagement.services.execution_tracker.claim_execution") as claim,
                    patch("apps.ai_engagement.tasks._execute_ai_engagement_response_impl") as execute,
                ):
                    messages.return_value.order_by.return_value.first.return_value = source
                    result = _execute_ai_engagement_response(task=Mock(), lead_id="lead-id")
                self.assertEqual(result["reason"], "nonconversational_inbound")
                self.assertEqual(result["status"], "skipped")
                record.assert_called_once_with("source-id", status="skipped", reason="nonconversational_inbound")
                claim.assert_not_called()
                execute.assert_not_called()

    def test_final_send_boundary_rejects_old_empty_notification_drafts(self):
        lead = SimpleNamespace(phone="+15551234567", organization_id="organization-id")
        account = SimpleNamespace(organization_id=lead.organization_id, is_active=True, status="connected")
        for payload in ({"type": "system"}, {"type": "unknown"}, {"type": "text", "text": {"body": ""}}):
            with self.subTest(payload=payload):
                source = SimpleNamespace(
                    direction="inbound", Direction=WhatsAppMessage.Direction, created_at=timezone.now(),
                    body="", raw_payload=payload, message_type="text",
                )
                self.assertEqual(_whatsapp_send_eligible(lead=lead, account=account, inbound_message=source),
                                 (False, "nonconversational_inbound"))

    def test_final_send_boundary_keeps_supported_media_eligible(self):
        lead = SimpleNamespace(phone="+15551234567", organization_id="organization-id")
        account = SimpleNamespace(organization_id=lead.organization_id, is_active=True, status="connected")
        for kind in ("image", "audio", "video", "document"):
            with self.subTest(kind=kind):
                source = SimpleNamespace(
                    direction="inbound", Direction=WhatsAppMessage.Direction, created_at=timezone.now(),
                    body="", raw_payload={"type": kind, kind: {"id": "provider-media"}}, message_type="text",
                )
                self.assertEqual(_whatsapp_send_eligible(lead=lead, account=account, inbound_message=source),
                                 (True, "eligible"))


class WhatsAppNonconversationalIngressTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Inbound Event Tests")
        self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization, connection_type="api", status="connected", is_active=True,
            phone_number_id="event-test-number", display_phone_number="+15551234567", business_name="API",
        )
        self.lead = Lead.objects.create(
            organization=self.organization, pipeline=self.pipeline,
            stage=self.pipeline.stages.order_by("display_order").first(),
            name="Customer", phone="+15559876543",
        )

    def inbound(self, payload):
        return handle_inbound_message(
            organization=self.organization, account=self.account, external_id=payload["id"],
            from_number="15559876543", to_number=self.account.display_phone_number,
            body="", raw_payload=payload,
        )

    @patch("services.channels.hosted_whatsapp_service.get_session_settings",
           return_value={"auto_lead_creation": False, "ai_auto_reply": True})
    @patch("services.channels.realtime.queue_message_publish")
    @patch("services.channels.whatsapp_service._queue_whatsapp_engagement")
    def test_verified_meta_delivery_records_notification_without_generating_reply(self, queue, _publish, _controls):
        import json
        from apps.channels.views_flat import _handle_webhook_delivery

        for index, payload in enumerate((
            {"type": "system", "system": {"type": "customer_changed_number"}},
            {"type": "unknown", "errors": [{"code": 131051}]},
            {"type": "unsupported"}, {"type": "text", "text": {"body": ""}},
        )):
            message = {"id": f"wamid.event.{index}", "from": "15559876543", **payload}
            request = RequestFactory().post("/webhook/", data=json.dumps({
                "entry": [{"changes": [{"value": {
                    "metadata": {"phone_number_id": self.account.phone_number_id}, "messages": [message],
                }}]}],
            }), content_type="application/json")
            with patch("apps.channels.views_flat._verify_signature", return_value=True):
                with self.captureOnCommitCallbacks(execute=True):
                    response = _handle_webhook_delivery(request)
            self.assertEqual(response.status_code, 200)
            stored = WhatsAppMessage.objects.get(external_id=message["id"])
            self.assertEqual(stored.raw_payload["type"], payload["type"])
            self.assertEqual(stored.body, "")
            self.assertEqual(stored.lead_id, self.lead.pk)
        queue.assert_not_called()

    @patch("services.channels.hosted_whatsapp_service.get_session_settings",
           return_value={"auto_lead_creation": False, "ai_auto_reply": True})
    @patch("services.channels.realtime.queue_message_publish")
    @patch("services.channels.whatsapp_service._queue_whatsapp_engagement")
    def test_supported_replies_and_media_captions_follow_existing_engagement_path(self, queue, _publish, _controls):
        for index, payload in enumerate((
            {"type": "text", "text": {"body": "What does Shvya do?"}},
            {"type": "button", "button": {"text": "Book a demo"}},
            {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"title": "Enterprise"}}},
            {"type": "document", "document": {"id": "media-id", "caption": "Please explain this quotation"}},
            {"type": "audio", "audio": {"id": "media-id"}},
        )):
            with self.captureOnCommitCallbacks(execute=True):
                source = self.inbound({"id": f"wamid.customer.{index}", **payload})
            self.assertEqual(source.body, extract_inbound_message_body(payload))
            queue.assert_called_with(lead_id=str(self.lead.pk), source_message_id=str(source.pk))
        self.assertEqual(queue.call_count, 5)

    @patch("services.channels.hosted_whatsapp_service.get_session_settings",
           return_value={"auto_lead_creation": False, "ai_auto_reply": True})
    @patch("services.channels.realtime.queue_message_publish")
    @patch("services.channels.whatsapp_service._queue_whatsapp_engagement")
    def test_blank_reply_retry_is_repaired_and_enters_engagement_once(self, queue, _publish, _controls):
        with self.captureOnCommitCallbacks(execute=True):
            first = self.inbound({"id": "wamid.repaired", "type": "button", "button": {}})
        queue.assert_not_called()
        payload = {"id": "wamid.repaired", "type": "button", "button": {"text": "Book a demo"}}
        with self.captureOnCommitCallbacks(execute=True):
            repaired = self.inbound(payload)
        with self.captureOnCommitCallbacks(execute=True):
            self.inbound(payload)
        self.assertEqual(repaired.pk, first.pk)
        queue.assert_called_once_with(lead_id=str(self.lead.pk), source_message_id=str(first.pk))

    @patch("services.channels.hosted_whatsapp_service.get_session_settings",
           return_value={"auto_lead_creation": False, "ai_auto_reply": True})
    @patch("services.channels.realtime.queue_message_publish")
    @patch("services.channels.whatsapp_service._queue_whatsapp_engagement")
    def test_repair_retains_processed_receipt_and_does_not_repeat_a_committed_turn(self, queue, _publish, _controls):
        source = WhatsAppMessage.objects.create(
            organization=self.organization, account=self.account, lead=self.lead, direction="inbound",
            external_id="wamid.processed", body="", status="received",
            raw_payload={"type": "button", "shvya_ai_processing": {"processed": True},
                         "shvya_ai_execution": {"status": "completed"}},
        )
        with self.captureOnCommitCallbacks(execute=True):
            repaired = self.inbound({"id": source.external_id, "type": "button", "button": {"text": "Yes"}})
        self.assertEqual(repaired.body, "Yes")
        self.assertTrue(repaired.raw_payload["shvya_ai_processing"]["processed"])
        self.assertEqual(repaired.raw_payload["shvya_ai_execution"]["status"], "completed")
        queue.assert_not_called()
