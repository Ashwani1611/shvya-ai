"""Hosted inbox regression coverage: real persistence, pagination and read races."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import HostedChatReadState, WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_chat_service import (
    build_hosted_chat_snapshot,
    handle_hosted_gateway_event,
    mark_hosted_chat_read,
)
from services.channels.hosted_message_content import repair_content_after_gateway_event
from services.channels.hosted_whatsapp_service import create_hosted_account


class HostedInboxReliabilityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Inbox reliability", settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="inbox-reliability@example.com", password="test-password",
            organization=self.org, role=User.Role.ADMIN, name="Admin",
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org, name="Sales", country_code="+91",
            phone_number="9319988591", owner=self.user,
        )
        self.stage, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, display_order=1, defaults={"name": "New Lead"},
        )
        self.account, _, _ = create_hosted_account(
            organization=self.org, created_by=self.user,
            country_code="+91", phone_number="9319988591",
        )
        self.account.status = WhatsAppAccount.Status.CONNECTED
        self.account.save(update_fields=["status", "updated_at"])
        self.phone = "+919812345678"
        self.now = timezone.now() - timedelta(hours=1)
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def message(self, index=0, *, phone=None, outbound=False, **kwargs):
        phone = phone or self.phone
        message = WhatsAppMessage.objects.create(
            organization=self.org, account=self.account,
            direction="outbound" if outbound else "inbound",
            from_number=self.account.display_phone_number if outbound else phone,
            to_number=phone if outbound else self.account.display_phone_number,
            body=f"Message {index}", is_read=outbound, **kwargs,
        )
        WhatsAppMessage.objects.filter(pk=message.pk).update(
            created_at=self.now + timedelta(seconds=index),
        )
        message.refresh_from_db()
        return message

    def snapshot(self, **kwargs):
        return build_hosted_chat_snapshot(
            account=self.account, selected_chat=self.phone, **kwargs,
        )

    def event(self, event="message", **kwargs):
        return {
            "sessionId": str(self.account.pk), "event": event,
            "messageId": "PROFILE-1", "from": "919812345678@c.us",
            "to": "919319988591@c.us", "peerPhone": self.phone,
            "peerKey": self.phone, "rawChatId": "12345678901@lid",
            "chatId": "12345678901@lid", "fromMe": False,
            "body": "Hello", "messageType": "text", "timestamp": int(self.now.timestamp()),
            **kwargs,
        }

    def test_latest_chat_order_uses_message_time_not_import_time(self):
        self.message(100, phone="+919888888888")
        self.message(1)
        self.assertEqual(self.snapshot()["conversations"][0]["key"], "+919888888888")

    def test_window_counts_all_unread_beyond_old_20000_row_limit(self):
        old = self.message(-1, phone="+919800000000")
        WhatsAppMessage.objects.bulk_create([
            WhatsAppMessage(
                organization=self.org, account=self.account, direction="inbound",
                from_number=self.phone, to_number=self.account.display_phone_number,
                body="Bulk", is_read=False,
            ) for _ in range(20005)
        ], batch_size=1000)
        snapshot = self.snapshot()
        counts = {row["key"]: row["unread"] for row in snapshot["conversations"]}
        self.assertEqual(counts, {self.phone: 20005, old.from_number: 1})
        self.assertEqual(len(snapshot["thread"]), 60)

    def test_keyset_pages_are_stable_with_equal_timestamps(self):
        messages = [self.message(i) for i in range(130)]
        WhatsAppMessage.objects.filter(account=self.account).update(created_at=self.now)
        seen = []
        before = ""
        lengths = []
        while True:
            page = self.snapshot(before=before)
            lengths.append(len(page["thread"]))
            seen.extend(str(message.pk) for message in page["thread"])
            if not page["has_more"]:
                break
            before = page["next_before"]
        self.assertEqual(lengths, [60, 60, 10])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {str(message.pk) for message in messages})

    def test_page_cursor_cannot_be_reused_for_another_chat_or_tampered(self):
        for i in range(61):
            self.message(i)
        before = self.snapshot()["next_before"]
        with self.assertRaises(ValueError):
            build_hosted_chat_snapshot(
                account=self.account, selected_chat="+919800000000", before=before,
            )
        with self.assertRaises(ValueError):
            self.snapshot(before=before + "tampered")

    def test_self_chat_does_not_include_every_incoming_conversation(self):
        self.message(1)
        own = self.message(2, phone=self.account.display_phone_number, outbound=True)
        page = build_hosted_chat_snapshot(
            account=self.account, selected_chat=self.account.display_phone_number,
        )
        self.assertEqual([message.pk for message in page["thread"]], [own.pk])

    def test_data_get_is_read_only_and_receipt_marks_only_observed_messages(self):
        old = self.message(1)
        url = reverse("whatsapp-hosted-session-chats-data", args=[self.account.pk])
        response = self.client.get(url, {"chat": self.phone})
        self.assertEqual(response.status_code, 200)
        old.refresh_from_db()
        self.assertFalse(old.is_read)
        token = response.json()["read_token"]
        # An old WhatsApp timestamp arriving after GET must still be unread.
        late = self.message(0)
        read_url = reverse("whatsapp-hosted-session-chat-read", args=[self.account.pk])
        response = self.client.post(read_url, {"token": token}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["marked_read"], 1)
        old.refresh_from_db()
        late.refresh_from_db()
        self.assertTrue(old.is_read)
        self.assertFalse(late.is_read)
        self.assertEqual(self.snapshot()["conversations"][0]["unread"], 1)
        self.assertEqual(mark_hosted_chat_read(account=self.account, token=token), 0)

    def test_read_receipt_cannot_cross_tenant_or_account(self):
        self.message()
        token = self.snapshot()["read_token"]
        other_org = Organization.objects.create(name="Other")
        other = WhatsAppAccount.objects.create(
            organization=other_org, connection_type="hosted", business_name="Other",
            phone_number_id="919811100000", display_phone_number="+919811100000",
        )
        with self.assertRaises(ValueError):
            mark_hosted_chat_read(account=other, token=token)
        url = reverse("whatsapp-hosted-session-chat-read", args=[other.pk])
        self.assertEqual(self.client.post(url, {"token": token}).status_code, 404)
        self.assertFalse(HostedChatReadState.objects.filter(account=other).exists())

    def test_history_unread_replay_respects_durable_read_watermark(self):
        item = self.event(isUnread=True)
        payload = {"sessionId": str(self.account.pk), "event": "history_sync", "messages": [item]}
        handle_hosted_gateway_event(payload=payload)
        self.assertEqual(self.snapshot()["conversations"][0]["unread"], 1)
        token = self.snapshot()["read_token"]
        mark_hosted_chat_read(account=self.account, token=token)
        handle_hosted_gateway_event(payload=payload)
        self.assertEqual(self.snapshot()["conversations"][0]["unread"], 0)
        self.assertFalse(Lead.objects.filter(organization=self.org).exists())

    def test_profile_name_is_saved_before_welcome_and_not_sent_twice(self):
        observed = []
        def welcome(lead):
            observed.append(Lead.objects.get(pk=lead.pk).name)
        payload = self.event(profileName="Public Profile", contactName="My Saved Nickname")
        with patch("services.crm.lead_service._schedule_new_lead_welcome", side_effect=welcome):
            handle_hosted_gateway_event(payload=payload)
            handle_hosted_gateway_event(payload=payload)
        self.assertEqual(observed, ["Public Profile"])
        self.assertEqual(Lead.objects.get(organization=self.org, phone=self.phone).name, "Public Profile")

    def test_stage_is_current_crm_data_even_if_messages_precede_lead(self):
        self.message(raw_payload={"contactName": "Public Profile"})
        lead = Lead.objects.create(
            organization=self.org, pipeline=self.pipeline, stage=self.stage,
            name="Curated Name", phone=self.phone, lead_source="manual",
        )
        qualified = Stage.objects.create(pipeline=self.pipeline, name="Qualified", display_order=9)
        lead.stage = qualified
        lead.save(update_fields=["stage", "updated_at"])
        row = self.snapshot()["conversations"][0]
        self.assertEqual(row["stage_name"], "Qualified")
        self.assertEqual(row["lead_id"], str(lead.pk))
        self.assertEqual(row["name"], "Curated Name")

    def test_ack_preserves_identity_media_timestamp_and_never_downgrades_read(self):
        message = self.message(
            outbound=True, external_id="wweb:ACK-1", status="sent",
            raw_payload={"timestamp": self.now.timestamp(), "peerKey": self.phone,
                         "contactName": "Profile", "filename": "document.pdf"},
            media_payload={"filename": "document.pdf"},
        )
        before = dict(message.raw_payload)
        for status in ["read", "sent", "delivered"]:
            handle_hosted_gateway_event(payload=self.event(
                "message_ack", messageId="ACK-1", status=status,
            ))
        message.refresh_from_db()
        self.assertEqual(message.status, "read")
        self.assertEqual({key: message.raw_payload[key] for key in before}, before)
        self.assertEqual(message.media_payload["filename"], "document.pdf")
        self.assertEqual(message.created_at, self.now)

    def test_history_content_repair_never_downgrades_live_provenance(self):
        payload = self.event(profileName="Profile")
        message = handle_hosted_gateway_event(payload=payload)
        history = {"sessionId": str(self.account.pk), "event": "history_sync", "messages": [payload]}
        handle_hosted_gateway_event(payload=history)
        repair_content_after_gateway_event(payload=history)
        message.refresh_from_db()
        self.assertFalse(message.raw_payload["isHistory"])
