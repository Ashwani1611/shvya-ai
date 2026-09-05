from datetime import timedelta
from types import SimpleNamespace

from django.test import RequestFactory, TestCase
from django.urls import resolve
from django.utils import timezone

from apps.channels.models import (
    BulkMessageCampaign,
    BulkMessageRecipient,
    WhatsAppAccount,
    WhatsAppMessage,
)
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import list_conversations


class WhatsAppInboxTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Inbox org")
        self.account = WhatsAppAccount.objects.create(organization=self.org)
        self.other_account = WhatsAppAccount.objects.create(organization=self.org)
        pipeline = Pipeline.objects.create(organization=self.org, name="Inbox")
        stage = Stage.objects.create(pipeline=pipeline, name="New")
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=pipeline,
            stage=stage,
            name="Inbox Lead",
            phone="+919876543210",
        )

    def message(
        self, *, account=None, direction="inbound", status="received", **kwargs
    ):
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=account or self.account,
            lead=self.lead,
            direction=direction,
            status=status,
            **kwargs,
        )

    def inbox(self, **kwargs):
        return list_conversations(organization=self.org, **kwargs)

    def test_inbox_route_renders_empty_and_populated_lists(self):
        # Exercise the real URL, view, service and template after CRM authentication.
        view = resolve("/dashboard/whatsapp/chats/").func.__wrapped__
        request = RequestFactory().get("/dashboard/whatsapp/chats/")
        request.crm_user = SimpleNamespace(organization=self.org)
        response = view(request)
        self.assertContains(response, "No conversations found")
        self.message(body="Hello inbox")
        response = view(request)
        self.assertContains(response, "Hello inbox")
        self.assertContains(response, "Inbox Lead")

    def test_unread_and_needs_reply_filters(self):
        self.message(body="Please help")
        self.assertEqual(self.inbox(tab="unread").count(), 1)
        self.assertEqual(self.inbox(tab="needs_reply").count(), 1)
        WhatsAppMessage.objects.update(is_read=True)
        self.assertFalse(self.inbox(tab="unread").exists())
        self.assertTrue(self.inbox(tab="needs_reply").exists())

    def test_latest_message_controls_preview_and_failed_tab(self):
        old = self.message(direction="outbound", status="failed", body="Old failure")
        WhatsAppMessage.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=1),
        )
        self.message(direction="outbound", status="sent", body="New reply")
        row = self.inbox(tab="all").get()
        self.assertEqual(row.last_msg_body, "New reply")
        self.assertEqual(row.last_msg_status, "sent")
        self.assertFalse(self.inbox(tab="failed").exists())
        self.assertFalse(self.inbox(tab="needs_reply").exists())

    def test_account_filter_scopes_previews_and_tabs(self):
        self.message(body="Question")
        self.message(
            account=self.other_account,
            direction="outbound",
            status="failed",
            body="Failed reply",
            error="Delivery failed",
        )
        row = self.inbox(account=self.account, tab="needs_reply").get()
        self.assertEqual(row.last_msg_body, "Question")
        self.assertEqual(row.unread_count, 1)
        self.assertFalse(self.inbox(account=self.account, tab="failed").exists())
        row = self.inbox(account=self.other_account, tab="failed").get()
        self.assertEqual(row.last_msg_error, "Delivery failed")
        self.assertFalse(self.inbox(account=self.other_account, tab="unread").exists())

    def test_other_organization_cannot_see_conversations(self):
        self.message(body="Private")
        other_org = Organization.objects.create(name="Other org")
        for tab in ("all", "unread", "needs_reply", "failed", "broadcasts"):
            with self.subTest(tab=tab):
                self.assertFalse(
                    list_conversations(organization=other_org, tab=tab).exists()
                )

    def test_broadcast_filter_uses_campaign_messages_for_selected_account(self):
        message = self.message(direction="outbound", status="sent")
        campaign = BulkMessageCampaign.objects.create(
            organization=self.org,
            account=self.account,
            pipeline=self.lead.pipeline,
            name="Campaign",
            body="Hello",
        )
        BulkMessageRecipient.objects.create(
            campaign=campaign, lead=self.lead, message=message
        )
        self.assertEqual(self.inbox(tab="broadcasts").get().pk, self.lead.pk)
        self.assertFalse(
            self.inbox(account=self.other_account, tab="broadcasts").exists()
        )
