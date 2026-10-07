"""Hosted delivery state-machine regressions; no live WhatsApp traffic."""
import inspect
import json
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation import tasks
from apps.organizations.models import Organization
from services.channels.hosted_automation_service import HostedAutomationPaused
from services.channels.hosted_whatsapp_transport import send_hosted_message
from services.channels.hosted_whatsapp_service import handle_gateway_event
from services.channels.whatsapp_service import WhatsAppSendError


class HostedTransportAcknowledgementTests(SimpleTestCase):
    def setUp(self):
        # Test the provider boundary independently of the already-covered pace
        # gate. All database/provider dependencies here are explicit doubles.
        self.send = inspect.unwrap(send_hosted_message)
        self.account = SimpleNamespace(
            id=uuid4(), organization_id=uuid4(), is_active=True,
            status=WhatsAppAccount.Status.CONNECTED, Status=WhatsAppAccount.Status,
        )
        self.message = SimpleNamespace(
            pk=uuid4(), account=self.account, organization_id=self.account.organization_id,
            account_id=self.account.id, lead_id=uuid4(), lead=Mock(),
            raw_payload={"shvya_welcome": {"trigger": "lead_created"}},
            status=WhatsAppMessage.Status.QUEUED, message_type=WhatsAppMessage.MessageType.TEXT,
            to_number="+919999999999", body="Test welcome", save=Mock(), error="",
            external_id="", refresh_from_db=Mock(),
        )
        self.client = Mock()
        self.enterContext(patch("apps.channels.hosted_gateway_routing.gateway_client_for_account", return_value=self.client))
        self.enterContext(patch("services.channels.hosted_whatsapp_service.account_ai_block_reason", return_value=""))
        self.enterContext(patch("services.channels.hosted_health_guard.message_is_hosted_automation", return_value=True))
        self.enterContext(patch("services.channels.hosted_health_guard.reserve_hosted_automation_send", return_value={"reserved": False}))
        self.finalize = self.enterContext(patch("services.channels.hosted_health_guard.finalize_hosted_send"))
        self.enterContext(patch("services.channels.hosted_whatsapp_transport._push_chat_refresh"))
        self.enterContext(patch("services.channels.hosted_whatsapp_transport.transaction.atomic", side_effect=nullcontext))

    @staticmethod
    def pending():
        return WhatsAppWebGatewayError(
            "Awaiting WhatsApp acknowledgement", status_code=503,
            response_body=json.dumps({"code": "provider_ack_pending", "messageId": "same-provider-id"}),
        )

    def test_pending_ack_keeps_same_generated_message_and_request_id(self):
        self.client.send_message.side_effect = [self.pending(), {
            "messageId": "same-provider-id", "ack": 2, "status": "delivered",
        }]
        with self.assertRaises(HostedAutomationPaused) as raised:
            self.send(message=self.message)
        self.assertEqual(raised.exception.reason, "provider_ack_pending")
        self.assertEqual(self.message.status, WhatsAppMessage.Status.QUEUED)
        self.assertEqual(self.message.external_id, "wweb:same-provider-id")
        self.finalize.assert_not_called()
        self.send(message=self.message)
        self.assertEqual(self.message.status, WhatsAppMessage.Status.DELIVERED)
        self.assertEqual(self.message.external_id, "wweb:same-provider-id")
        calls = self.client.send_message.call_args_list
        self.assertEqual(calls[0].kwargs["request_id"], calls[1].kwargs["request_id"])
        self.assertFalse(calls[0].kwargs["request_is_retry"])
        self.assertTrue(calls[1].kwargs["request_is_retry"])
        self.finalize.assert_called_once()

    def test_authenticated_ack_winning_pending_response_keeps_delivery_confirmed(self):
        self.client.send_message.side_effect = self.pending()

        def receive_ack():
            self.message.status = WhatsAppMessage.Status.DELIVERED
            self.message.error = ""

        self.message.refresh_from_db.side_effect = receive_ack
        result = self.send(message=self.message)
        self.assertIs(result, self.message)
        self.assertEqual(result.status, WhatsAppMessage.Status.DELIVERED)
        self.finalize.assert_called_once()
        self.client.send_message.assert_called_once()

    def test_ack_after_initial_refresh_prevents_timeout_and_retry_error_writes(self):
        for elapsed in (0, 301):
            with self.subTest(elapsed=elapsed):
                self.message.status = WhatsAppMessage.Status.QUEUED
                self.message.error = ""
                self.message.raw_payload["shvya_hosted_request"] = {
                    "request_id": str(self.message.pk),
                    "attempted_at": (timezone.now() - timedelta(seconds=elapsed)).isoformat(),
                }
                self.message.save.reset_mock()
                self.finalize.reset_mock()
                self.client.send_message.reset_mock()
                self.client.send_message.side_effect = self.pending()

                def refresh(*, from_queryset=None):
                    # The first refresh still sees queued. An ACK commits
                    # before the final, locked read of the error write path.
                    if from_queryset is not None:
                        self.message.status = WhatsAppMessage.Status.DELIVERED
                        self.message.error = ""

                self.message.refresh_from_db.side_effect = refresh
                result = self.send(message=self.message)
                self.assertEqual(result.status, WhatsAppMessage.Status.DELIVERED)
                self.assertEqual(result.error, "")
                self.assertFalse(any(
                    "error" in call.kwargs.get("update_fields", [])
                    or "status" in call.kwargs.get("update_fields", [])
                    for call in self.message.save.call_args_list
                ))
                self.finalize.assert_called_once()
                self.client.send_message.assert_called_once()

    def test_pending_ack_cannot_replace_a_different_provider_identity(self):
        self.message.external_id = "wweb:original-id"
        self.client.send_message.side_effect = self.pending()
        with self.assertRaisesRegex(WhatsAppSendError, "identity changed"):
            self.send(message=self.message)
        self.assertEqual(self.message.external_id, "wweb:original-id")
        self.assertEqual(self.message.status, WhatsAppMessage.Status.QUEUED)

    def test_pending_provider_id_is_resolved_by_scoped_ack_callback(self):
        self.client.send_message.side_effect = self.pending()
        with self.assertRaises(HostedAutomationPaused):
            self.send(message=self.message)
        self.account.organization = Mock()
        self.message.direction = "outbound"
        self.message.sent_at = None

        def scoped_message(**filters):
            self.assertEqual(filters, {
                "organization": self.account.organization, "account": self.account,
                "external_id": self.message.external_id,
            })
            return Mock(first=Mock(return_value=self.message))

        with (
            patch.object(WhatsAppAccount.objects, "select_related") as accounts,
            patch.object(WhatsAppMessage.objects, "select_for_update", return_value=Mock(filter=scoped_message)),
            patch("apps.channels.hosted_gateway_routing.record_gateway_presence", return_value=True),
        ):
            accounts.return_value.filter.return_value.first.return_value = self.account
            result = inspect.unwrap(handle_gateway_event)(payload={
                "event": "message_ack", "sessionId": str(self.account.id),
                "messageId": "same-provider-id", "status": "delivered",
            })
        self.assertIs(result, self.message)
        self.assertEqual(result.status, "delivered")
        self.assertEqual(result.error, "")
        self.assertIsNotNone(result.sent_at)
        self.assertIn("shvya_welcome", result.raw_payload)
        self.client.send_message.assert_called_once()

    def test_ack_wait_is_bounded_and_never_claims_success(self):
        self.message.raw_payload["shvya_hosted_request"] = {
            "request_id": str(self.message.pk),
            "attempted_at": (timezone.now() - timedelta(seconds=301)).isoformat(),
        }
        self.client.send_message.side_effect = self.pending()
        with self.assertRaisesRegex(WhatsAppSendError, "Delivery is unconfirmed"):
            self.send(message=self.message)
        self.assertEqual(self.message.status, WhatsAppMessage.Status.FAILED)
        self.assertTrue(self.client.send_message.call_args.kwargs["request_is_retry"])
        self.finalize.assert_not_called()

    def test_read_ack_is_not_downgraded_to_sent(self):
        self.client.send_message.return_value = {"messageId": "read-id", "ack": 3, "status": "read"}
        self.send(message=self.message)
        self.assertEqual(self.message.status, WhatsAppMessage.Status.READ)

    def test_disconnect_defers_automation_without_calling_provider(self):
        self.account.status = WhatsAppAccount.Status.DISCONNECTED
        with self.assertRaises(HostedAutomationPaused) as raised:
            self.send(message=self.message)
        self.assertEqual(raised.exception.reason, "session_reconnecting")
        self.assertEqual(self.message.status, WhatsAppMessage.Status.QUEUED)
        self.client.send_message.assert_not_called()


class HostedPendingAckReconciliationTests(TestCase):
    def test_pending_response_allows_callback_to_confirm_original_row_without_resend(self):
        organization = Organization.objects.create(name="Hosted ACK reconciliation")
        account = WhatsAppAccount.objects.create(
            organization=organization, connection_type="hosted",
            status=WhatsAppAccount.Status.CONNECTED, is_active=True,
        )
        pipeline = Pipeline.objects.create(
            organization=organization, name="Hosted ACK", country_code="+91", phone_number="9000000991",
        )
        lead = Lead.objects.create(
            organization=organization, pipeline=pipeline, stage=pipeline.stages.first(),
            name="Test lead", phone="+919999999999",
        )
        message = WhatsAppMessage.objects.create(
            organization=organization, account=account, lead=lead, direction="outbound",
            status="queued", body="AI reply", to_number="+919999999999",
            raw_payload={"shvya_welcome": {"trigger": "lead_created"}},
        )
        with (
            patch("apps.channels.hosted_gateway_routing.gateway_client_for_account") as gateway,
            patch("services.channels.hosted_whatsapp_service.account_ai_block_reason", return_value=""),
            patch("services.channels.hosted_health_guard.reserve_hosted_automation_send", return_value={"reserved": False}),
            patch("services.channels.hosted_health_guard.finalize_hosted_send"),
            patch("services.channels.hosted_whatsapp_transport._push_chat_refresh"),
            patch("apps.channels.hosted_gateway_routing.record_gateway_presence", return_value=True),
        ):
            gateway.return_value.send_message.side_effect = HostedTransportAcknowledgementTests.pending()
            with self.assertRaises(HostedAutomationPaused):
                inspect.unwrap(send_hosted_message)(message=message)
            message.refresh_from_db()
            self.assertEqual(message.status, "queued")
            self.assertEqual(message.external_id, "wweb:same-provider-id")
            self.assertIsNone(message.sent_at)
            result = handle_gateway_event(payload={
                "event": "message_ack", "sessionId": str(account.pk),
                "messageId": "same-provider-id", "status": "delivered",
            })
            self.assertEqual(result.pk, message.pk)
            message.refresh_from_db()
            self.assertEqual(message.status, "delivered")
            self.assertEqual(message.error, "")
            self.assertIsNotNone(message.sent_at)
            self.assertIn("shvya_welcome", message.raw_payload)
            job = SimpleNamespace(
                result={"message_id": str(message.pk)}, organization=organization, account=account,
            )
            self.assertEqual(tasks._send_generated_ai_message(job)["status"], "sent")
            gateway.return_value.send_message.assert_called_once()


class HostedDurableReconnectTests(SimpleTestCase):
    def test_disconnect_does_not_cancel_the_generated_reply(self):
        source = SimpleNamespace(pk=uuid4(), raw_payload={})
        lead = Mock()
        lead.whatsapp_messages.filter.return_value.order_by.return_value.first.return_value = source
        job = SimpleNamespace(
            result={"message_id": str(uuid4())}, kind="engagement", source_message=source,
            source_message_id=source.pk, lead=lead, organization=Mock(),
            account=SimpleNamespace(connection_type=WhatsAppAccount.ConnectionType.coexisted, is_active=True),
        )
        original_result = dict(job.result)
        with (
            patch.object(tasks, "_recover_message"),
            patch.object(tasks.WhatsAppMessage.objects, "filter") as messages,
            patch.object(tasks, "hosted_ai_block_reason", return_value="whatsapp_account_not_connected"),
            patch.object(tasks, "_defer", return_value={"status": "deferred"}) as defer,
            patch.object(tasks, "_cancel_generated_message") as cancel,
            patch.object(tasks, "_send_generated_ai_message") as send,
        ):
            messages.return_value.exists.return_value = False
            self.assertEqual(tasks._execute(job), {"status": "deferred"})
        self.assertEqual(defer.call_args.args[2], "session_reconnecting")
        self.assertEqual(job.result, original_result)
        cancel.assert_not_called()
        send.assert_not_called()

    def test_ack_checks_do_not_consume_generation_retry_budget(self):
        job = Mock()
        pause = HostedAutomationPaused(timezone.now() + timedelta(seconds=15))
        pause.reason = "provider_ack_pending"
        with (
            patch.object(tasks, "_claim", return_value=(job, None)),
            patch.object(tasks, "_execute", side_effect=pause),
            patch.object(tasks, "_defer") as defer,
        ):
            tasks.process_hosted_ai_engagement_job_task.run(str(uuid4()))
        self.assertFalse(defer.call_args.kwargs["retry"])
        self.assertEqual(defer.call_args.args[2], "provider_ack_pending")
