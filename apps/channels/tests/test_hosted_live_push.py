"""Committed Hosted inbox events; no live WhatsApp traffic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.layers import InMemoryChannelLayer
from django.db import transaction
from django.test import TestCase, SimpleTestCase

from apps.channels.hosted_consumers import HostedWhatsAppChatConsumer
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.organizations.models import Organization
from services.channels.hosted_chat_realtime import broadcast_committed_message
from services.channels.message_visibility import pending_ai_message_q


class HostedLivePushTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Live inbox", package="dfy")
        self.account = WhatsAppAccount.objects.create(
            organization=self.org, connection_type="hosted", is_active=True,
            business_name="Hosted", phone_number_id="+919000000000",
            display_phone_number="+919000000000", status="connected",
        )
        self.layer = SimpleNamespace(group_send=AsyncMock())
        self.enterContext(patch("services.channels.hosted_chat_realtime.get_channel_layer", return_value=self.layer))

    def make_message(self, **kwargs):
        values = dict(
            organization=self.org, account=self.account, direction="inbound",
            from_number="+919999999999", to_number=self.account.display_phone_number,
            body="Hello", status="received", raw_payload={},
        )
        values.update(kwargs)
        return WhatsAppMessage.objects.create(**values)

    def events(self):
        return [call.args[1] for call in self.layer.group_send.await_args_list]

    def test_inbound_is_published_only_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            message = self.make_message()
            self.layer.group_send.assert_not_called()
        event = self.events()[-1]
        self.assertEqual(event["type"], "hosted.message")
        self.assertEqual(event["message"]["id"], str(message.pk))
        self.assertEqual(event["message"]["body"], "Hello")
        self.assertEqual(event["chat_key"], "+919999999999")
        self.assertEqual(self.layer.group_send.call_args.args[0], f"hosted_whatsapp_{self.account.pk}")

    def test_transaction_rollback_publishes_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.make_message()
                raise RuntimeError("rollback")
        self.layer.group_send.assert_not_called()

    def test_queued_ai_and_send_statuses_publish_without_provider_calls(self):
        with self.captureOnCommitCallbacks(execute=True):
            message = self.make_message(direction="outbound", status="queued",
                from_number=self.account.display_phone_number, to_number="+919999999999",
                raw_payload={"shvya_ai": {"job_id": "test-job"}})
        self.assertEqual(self.events()[-1]["message"]["status"], "queued")
        for status in ("sending", "sent", "delivered", "read"):
            with self.captureOnCommitCallbacks(execute=True):
                message.status = status
                message.save(update_fields=["status", "updated_at"])
            self.assertEqual(self.events()[-1]["message"]["status"], status)

    def test_final_committed_identity_is_used(self):
        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            message = self.make_message(from_number="unknown")
            message.from_number = "+919999999999"
            message.raw_payload = {"peerPhone": "+919999999999", "rawChatId": "100000000@lid"}
            message.save(update_fields=["from_number", "raw_payload", "updated_at"])
        self.assertTrue(self.events())
        for event in self.events():
            self.assertEqual(event["chat_key"], "+919999999999")
            self.assertIn("100000000@lid", event["aliases"])

    def test_history_and_other_provider_do_not_publish(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_message(raw_payload={"isHistory": True})
        self.layer.group_send.assert_not_called()
        self.account.connection_type = "api"
        self.account.save(update_fields=["connection_type"])
        with self.captureOnCommitCallbacks(execute=True):
            self.make_message()
        self.layer.group_send.assert_not_called()

    def test_tenant_mismatch_is_not_broadcast(self):
        other = Organization.objects.create(name="Other", package="dfy")
        message = self.make_message(organization=other)
        broadcast_committed_message(message.pk)
        self.layer.group_send.assert_not_called()

    def test_transport_payloads_are_never_in_socket_content(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_message(message_type="image", body="data:image/png;base64,SECRET",
                raw_payload={"access_token": "TOP_SECRET", "filename": "photo.png"})
        event = self.events()[-1]
        self.assertEqual(event["message"]["body"], "")
        self.assertIn("media_url", event["message"])
        self.assertNotIn("SECRET", str(event))
        self.assertNotIn("raw_payload", str(event))

    def test_cancelled_unsent_ai_publishes_removal(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_message(direction="outbound", status="failed",
                raw_payload={"shvya_ai": {}}, error="AI send cancelled: source changed")
        event = self.events()[-1]
        self.assertEqual(event["operation"], "remove")
        self.assertNotIn("message", event)

    def test_redis_error_does_not_change_committed_delivery_status(self):
        self.layer.group_send.side_effect = ConnectionError("Redis down")
        with self.assertLogs("services.channels.hosted_chat_realtime", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                message = self.make_message(direction="outbound", status="sent")
        message.refresh_from_db()
        self.assertEqual(message.status, "sent")

    def test_visibility_keeps_api_policy_and_shows_hosted_pending(self):
        message = self.make_message(direction="outbound", status="queued", raw_payload={"shvya_ai": {}})
        self.assertFalse(WhatsAppMessage.objects.filter(pk=message.pk).filter(pending_ai_message_q()).exists())
        self.account.connection_type = "api"
        self.account.save(update_fields=["connection_type"])
        self.assertTrue(WhatsAppMessage.objects.filter(pk=message.pk).filter(pending_ai_message_q()).exists())


class HostedSubscriptionTests(SimpleTestCase):
    def test_subscription_recovers_after_channel_layer_loses_memberships(self):
        async def check():
            layer = InMemoryChannelLayer()
            consumer = HostedWhatsAppChatConsumer()
            consumer.channel_layer = layer
            consumer.channel_name = await layer.new_channel()
            consumer.account_id = "account"
            consumer.organization_id = "org"
            consumer.group_name = "hosted_whatsapp_account"
            consumer._account_belongs_to_org = AsyncMock(return_value=True)
            await consumer._renew_subscription()
            await layer.flush()
            self.assertTrue(await consumer._renew_subscription())
            await layer.group_send(consumer.group_name, {"type": "hosted.refresh"})
            self.assertEqual((await layer.receive(consumer.channel_name))["type"], "hosted.refresh")
        async_to_sync(check)()

    def test_cross_account_events_are_not_forwarded(self):
        async def check():
            consumer = HostedWhatsAppChatConsumer()
            consumer.account_id, consumer.organization_id = "account", "org"
            consumer.send_json = AsyncMock()
            await consumer.hosted_message({"account_id": "other", "organization_id": "org"})
            await consumer.hosted_message({"account_id": "account", "organization_id": "other"})
            consumer.send_json.assert_not_called()
        async_to_sync(check)()

    def test_account_access_is_checked_before_subscription_renewal(self):
        async def check():
            consumer = HostedWhatsAppChatConsumer()
            consumer.account_id, consumer.organization_id = "account", "org"
            consumer.channel_layer = SimpleNamespace(group_add=AsyncMock())
            consumer._account_belongs_to_org = AsyncMock(return_value=False)
            consumer.close = AsyncMock()
            self.assertFalse(await consumer._renew_subscription())
            consumer.close.assert_awaited_once_with(code=4003)
            consumer.channel_layer.group_add.assert_not_called()
        async_to_sync(check)()
