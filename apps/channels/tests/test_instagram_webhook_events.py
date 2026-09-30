"""Webhook retries, account routing, and destructive message updates."""
from datetime import timedelta
import hashlib
import json

from django.test import TestCase
from django.utils import timezone

from apps.channels.instagram_models import (
    InstagramAccount, InstagramConversation, InstagramMessage, InstagramWebhookDelivery,
)
from apps.organizations.models import Organization
from services.channels import instagram_service as service
from services.channels.instagram_inbox import conversation_policy, serialize_message


class InstagramWebhookEventTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Webhook event workspace")
        self.account = InstagramAccount.objects.create(
            organization=self.organization, ig_user_id="professional-account",
            access_token="test-token", status=InstagramAccount.Status.CONNECTED,
            token_expires_at=timezone.now() + timedelta(days=30),
        )
        self.conversation = InstagramConversation.objects.create(
            organization=self.organization, account=self.account,
            participant_id="customer", participant_name="Known customer",
        )
        self.now = timezone.now()
        self.envelope_sequence = 0

    def message(self, mid, *, direction="inbound", conversation=None, sent_at=None, **kwargs):
        conversation = conversation or self.conversation
        account = conversation.account
        outbound = direction == "outbound"
        defaults = {
            "organization": conversation.organization, "account": account,
            "conversation": conversation, "external_id": mid, "direction": direction,
            "status": "sent" if outbound else "received", "body": mid,
            "sender_id": account.ig_user_id if outbound else conversation.participant_id,
            "recipient_id": conversation.participant_id if outbound else account.ig_user_id,
            "is_read": outbound, "sent_at": sent_at or self.now,
        }
        defaults.update(kwargs)
        return InstagramMessage.objects.create(**defaults)

    def event(self, mid=None, *, outbound=False, **kwargs):
        result = {
            "sender": {"id": self.account.ig_user_id if outbound else "customer"},
            "recipient": {"id": "customer" if outbound else self.account.ig_user_id},
            "timestamp": int(self.now.timestamp() * 1000),
        }
        if mid:
            result["message"] = {"mid": mid, "text": "Original text"}
        result.update(kwargs)
        return result

    def deliver(self, events):
        self.envelope_sequence += 1
        payload = {"object": "instagram", "entry": [{
            "id": self.account.ig_user_id, "time": self.envelope_sequence, "messaging": events,
        }]}
        delivery = InstagramWebhookDelivery.objects.create(
            raw_payload=payload,
            payload_sha256=hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
        )
        count = service.process_webhook_delivery(delivery)
        delivery.refresh_from_db()
        return count, delivery

    def test_unsend_removes_content_and_unread_count_without_changing_send_time(self):
        original = self.message(
            "deleted-mid", body="Private original", sent_at=self.now - timedelta(hours=25),
            attachments=[{"type": "image", "payload": {"url": "https://example.com/private.jpg"}}],
            raw_payload={"message": {"mid": "deleted-mid", "text": "Private original"}},
        )
        count, delivery = self.deliver([self.event(
            message={"mid": original.external_id, "is_deleted": True},
        )])
        self.assertEqual(count, 1)
        original.refresh_from_db()
        self.conversation.refresh_from_db()
        self.assertEqual(original.body, "Message deleted on Instagram")
        self.assertEqual(original.attachments, [])
        self.assertNotIn("Private original", json.dumps(original.raw_payload))
        self.assertEqual(original.sent_at, self.now - timedelta(hours=25))
        self.assertTrue(serialize_message(original)["deleted"])
        self.assertEqual(serialize_message(original)["media"], [])
        self.assertEqual(self.conversation.unread_count, 0)
        self.assertFalse(conversation_policy(self.conversation)["can_reply"])
        self.assertEqual(service.process_webhook_delivery(delivery), 0)

    def test_unsend_arriving_before_original_creates_tombstone_without_reply_window(self):
        self.deliver([self.event(message={"mid": "late-original", "is_deleted": True})])
        tombstone = InstagramMessage.objects.get(external_id="late-original")
        self.assertIsNone(tombstone.sent_at)
        self.assertTrue(tombstone.is_read)
        self.assertFalse(conversation_policy(self.conversation)["can_reply"])
        self.deliver([self.event("late-original")])
        service._upsert_graph_message(
            account=self.account, conversation=self.conversation,
            item={"id": "late-original", "from": {"id": "customer"},
                  "message": "Graph copy of removed text", "created_time": self.now.isoformat()},
        )
        tombstone.refresh_from_db()
        self.assertEqual(InstagramMessage.objects.filter(external_id="late-original").count(), 1)
        self.assertTrue(serialize_message(tombstone)["deleted"])
        self.assertEqual(tombstone.body, "Message deleted on Instagram")
        self.assertIsNone(tombstone.sent_at)

    def test_deleting_older_message_keeps_newer_preview_and_participant_name(self):
        self.message("older", sent_at=self.now - timedelta(hours=1))
        self.message("newer")
        self.deliver([self.event(message={"mid": "older", "is_deleted": True})])
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.last_message_text, "newer")
        self.assertEqual(self.conversation.last_message_at, self.now)
        self.assertEqual(self.conversation.participant_name, "Known customer")
        self.assertEqual(self.conversation.unread_count, 1)

    def test_echo_does_not_downgrade_read_or_mutate_another_workspace(self):
        own = self.message("own-read", direction="outbound", status="read")
        foreign_org = Organization.objects.create(name="Other webhook workspace")
        foreign_account = InstagramAccount.objects.create(
            organization=foreign_org, ig_user_id="another-account", access_token="other-token",
            status="connected",
        )
        foreign_conversation = InstagramConversation.objects.create(
            organization=foreign_org, account=foreign_account, participant_id="foreign-customer",
        )
        foreign = self.message("foreign-mid", direction="outbound", conversation=foreign_conversation, status="failed")
        self.deliver([
            self.event("own-read", outbound=True),
            self.event("foreign-mid", outbound=True),
            self.event(outbound=True, message={"mid": "foreign-mid", "is_deleted": True}),
        ])
        own.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(own.status, "read")
        self.assertEqual(foreign.status, "failed")
        self.assertEqual(foreign.body, "foreign-mid")

    def test_mismatched_recipient_and_inbound_echo_are_not_inbound_messages(self):
        count, _ = self.deliver([
            self.event("wrong-route", recipient={"id": "unrelated-account"}),
            self.event(message={"mid": "inbound-echo", "text": "Echo", "is_echo": True}),
            self.event("self-send", sender={"id": self.account.ig_user_id}),
        ])
        self.assertEqual(count, 0)
        self.assertEqual(InstagramMessage.objects.count(), 0)

    def test_mid_receipt_only_marks_named_message_and_keeps_newer_reply_sent(self):
        read = self.message("read-mid", direction="outbound", sent_at=self.now - timedelta(minutes=1))
        newer = self.message("newer-mid", direction="outbound")
        count, _ = self.deliver([self.event(read={"mid": "read-mid"})])
        self.assertEqual(count, 1)
        read.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(read.status, "read")
        self.assertEqual(newer.status, "sent")

    def test_invalid_receipt_does_not_mark_entire_thread_read(self):
        message = self.message("not-read", direction="outbound")
        self.deliver([
            self.event(read={"mid": "unknown-mid"}),
            self.event(read={"watermark": "not-a-time"}),
            self.event(read={"watermark": 10 ** 100}),
            self.event(outbound=True, read={"mid": "not-read"}),
        ])
        message.refresh_from_db()
        self.assertEqual(message.status, "sent")

    def test_legacy_watermark_receipt_is_bounded_by_event_watermark(self):
        older = self.message("older-outbound", direction="outbound", sent_at=self.now - timedelta(minutes=2))
        newer = self.message("newer-outbound", direction="outbound")
        self.deliver([self.event(read={"watermark": int((self.now - timedelta(minutes=1)).timestamp() * 1000)})])
        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(older.status, "read")
        self.assertEqual(newer.status, "sent")

    def test_receipt_for_unknown_participant_does_not_create_empty_thread(self):
        self.deliver([self.event(sender={"id": "unknown-customer"}, read={"mid": "untracked"})])
        self.assertEqual(InstagramConversation.objects.count(), 1)

    def test_malformed_events_do_not_drop_valid_events_batched_with_them(self):
        count, delivery = self.deliver([
            None, "invalid", {"sender": "not-an-object"},
            self.event("valid-after-malformed"),
        ])
        self.assertEqual(count, 1)
        self.assertEqual(delivery.status, "processed")
        self.assertTrue(InstagramMessage.objects.filter(external_id="valid-after-malformed").exists())

    def test_late_inbound_delivery_does_not_regress_latest_conversation_preview(self):
        self.message("newer-message")
        self.deliver([self.event("older-message", timestamp=int((self.now - timedelta(hours=2)).timestamp() * 1000))])
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.last_message_text, "newer-message")
        self.assertEqual(self.conversation.last_message_at, self.now)
        self.assertEqual(self.conversation.unread_count, 2)

    def test_duplicate_mid_cannot_move_between_participants_in_one_workspace(self):
        original = self.message("original-route")
        other = InstagramConversation.objects.create(
            organization=self.organization, account=self.account, participant_id="other-customer",
        )
        self.deliver([self.event(sender={"id": "other-customer"}, message={"mid": "original-route", "is_deleted": True})])
        original.refresh_from_db()
        self.assertEqual(original.body, "original-route")
        self.assertEqual(original.conversation_id, self.conversation.pk)
        self.assertEqual(other.messages.count(), 0)
