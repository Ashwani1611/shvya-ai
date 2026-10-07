"""Worker interruption must not leave manual sends in-flight indefinitely."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.tasks import reconcile_hosted_sessions
from apps.organizations.models import Organization
from services.channels.hosted_send_service import expire_abandoned_hosted_manual_sends
from services.channels.hosted_whatsapp_service import handle_gateway_event


class HostedAbandonedManualSendTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Interrupted Hosted send")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization, connection_type="hosted",
            status=WhatsAppAccount.Status.CONNECTED, is_active=True,
        )
        self.now = timezone.now()
        self.refresh = self.enterContext(patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh"))
        self.provider = self.enterContext(patch("apps.channels.hosted_gateway_routing.gateway_client_for_account"))
        self.publish = self.enterContext(patch("apps.channels.hosted_send_tasks.send_hosted_whatsapp_message_task.apply_async"))

    def message(self, *, stale=True, **changes):
        values = {
            "organization": self.organization, "account": self.account,
            "direction": "outbound", "status": "sending", "to_number": "+919000000002",
            "body": "Interrupted manual reply",
            "raw_payload": {"shvya_hosted": {"origin": "agent"}},
        }
        values.update(changes)
        message = WhatsAppMessage.objects.create(**values)
        if stale:
            WhatsAppMessage.objects.filter(pk=message.pk).update(updated_at=self.now - timedelta(minutes=11))
        return message

    def test_abandoned_send_is_explicitly_uncertain_and_never_replayed(self):
        message = self.message(external_id="wweb:original-send")
        self.assertEqual(expire_abandoned_hosted_manual_sends(now=self.now), 1)
        message.refresh_from_db()
        self.assertEqual(message.status, "failed")
        self.assertIn("unconfirmed", message.error)
        self.assertEqual(message.external_id, "wweb:original-send")
        self.assertEqual(message.raw_payload["shvya_hosted"]["origin"], "agent")
        self.refresh.assert_called_once()
        self.provider.assert_not_called()
        self.publish.assert_not_called()

        # Expiry preserves the original provider identity for a late receipt.
        with patch("apps.channels.hosted_gateway_routing.record_gateway_presence", return_value=True):
            handle_gateway_event(payload={
                "sessionId": str(self.account.pk), "event": "message_ack",
                "messageId": "original-send", "status": "delivered",
            })
        message.refresh_from_db()
        self.assertEqual(message.status, "delivered")
        self.assertEqual(message.error, "")

    def test_recovery_leaves_current_work_automation_and_api_messages_alone(self):
        retained = [self.message(stale=False)]
        for status in ("queued", "sent", "delivered", "read"):
            retained.append(self.message(status=status))
        for marker in ("shvya_ai", "shvya_welcome", "shvya_auto_followup", "shvya_workflow", "shvya_sales"):
            retained.append(self.message(raw_payload={"shvya_hosted": {"origin": "agent"}, marker: {}}))
        api = WhatsAppAccount.objects.create(
            organization=self.organization, connection_type="api",
            status=WhatsAppAccount.Status.CONNECTED, is_active=True,
        )
        retained.append(self.message(account=api))
        retained.append(self.message(direction="inbound"))
        statuses = {row.pk: row.status for row in retained}
        self.assertEqual(expire_abandoned_hosted_manual_sends(now=self.now), 0)
        self.assertEqual(dict(WhatsAppMessage.objects.values_list("pk", "status")), statuses)
        self.provider.assert_not_called()
        self.publish.assert_not_called()

    def test_recovery_batch_is_bounded(self):
        self.message()
        self.message()
        self.assertEqual(expire_abandoned_hosted_manual_sends(now=self.now, limit=1), 1)
        self.assertEqual(WhatsAppMessage.objects.filter(status="sending").count(), 1)
        self.assertEqual(expire_abandoned_hosted_manual_sends(now=self.now, limit=1), 1)

    def test_existing_periodic_reconciliation_runs_manual_recovery(self):
        self.message()
        self.provider.return_value.get_session.return_value = {"status": "running"}
        with patch("apps.channels.hosted_gateway_routing.record_gateway_presence", return_value=True):
            result = reconcile_hosted_sessions()
        self.assertEqual(result["expired_manual_sends"], 1)
        self.assertEqual(WhatsAppMessage.objects.get().status, "failed")
        self.publish.assert_not_called()
