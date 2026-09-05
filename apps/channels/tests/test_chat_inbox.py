from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import list_conversations


class WhatsAppChatInboxTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Inbox Org")
        self.user = User.objects.create_user(
            email="inbox@example.com", password="test-password",
            name="Inbox Admin", organization=self.org, role=User.Role.ADMIN,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org, phone_number_id="inbox-1",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = Stage.objects.create(pipeline=self.pipeline, name="New")
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def make_lead(self, name, phone):
        return Lead.objects.create(
            organization=self.org, pipeline=self.pipeline, stage=self.stage,
            name=name, phone=phone,
        )

    def make_message(self, lead, **kwargs):
        values = {
            "organization": self.org, "account": self.account, "lead": lead,
            "direction": WhatsAppMessage.Direction.INBOUND,
            "status": WhatsAppMessage.Status.RECEIVED,
            "body": "Hello from WhatsApp", "is_read": False,
            "from_number": lead.phone, "to_number": "+919000000000",
        }
        values.update(kwargs)
        return WhatsAppMessage.objects.create(**values)

    def test_empty_inbox_renders(self):
        response = self.client.get("/dashboard/whatsapp/chats/")
        self.assertContains(response, "No conversations found")
        self.assertTemplateUsed(response, "channels/whatsapp_chat_list.html")

    def test_populated_inbox_and_thread_render(self):
        lead = self.make_lead("Inbox Lead", "+919000000001")
        self.make_message(lead)
        response = self.client.get(reverse("whatsapp-chats"))
        self.assertContains(response, "Hello from WhatsApp")
        self.assertEqual(response.context["tab_counts"]["unread"], 1)
        response = self.client.get(reverse("whatsapp-chat-detail", args=[lead.pk]))
        self.assertContains(response, "Hello from WhatsApp")

    def test_tabs_use_latest_message_and_unread_count(self):
        waiting = self.make_lead("Waiting", "+919000000002")
        failed = self.make_lead("Failed", "+919000000003")
        answered = self.make_lead("Answered", "+919000000004")
        self.make_message(waiting)
        self.make_message(failed, direction="outbound", status="failed", is_read=True)
        self.make_message(answered, is_read=True)
        self.make_message(answered, direction="outbound", status="sent", is_read=True)
        expected = {
            "all": {waiting.pk, failed.pk, answered.pk},
            "unread": {waiting.pk}, "needs_reply": {waiting.pk},
            "failed": {failed.pk}, "broadcasts": set(),
        }
        for tab, ids in expected.items():
            with self.subTest(tab=tab):
                response = self.client.get(reverse("whatsapp-chats"), {"tab": tab})
                self.assertEqual(response.status_code, 200)
                self.assertEqual({lead.pk for lead in response.context["conversations"]}, ids)

    def test_account_filter_scopes_preview_and_counts(self):
        lead = self.make_lead("Two numbers", "+919000000005")
        self.make_message(lead, body="First account")
        second = WhatsAppAccount.objects.create(
            organization=self.org, phone_number_id="inbox-2",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.make_message(
            lead, account=second, body="Second account", direction="outbound",
            status="failed", is_read=True,
        )
        conversation = list_conversations(
            organization=self.org, account=self.account, tab="needs_reply",
        ).get()
        self.assertEqual(conversation.last_msg_body, "First account")
        self.assertEqual(conversation.last_msg_direction, "inbound")
        self.assertEqual(conversation.last_msg_status, "received")
        self.assertEqual(conversation.unread_count, 1)
        self.assertFalse(list_conversations(
            organization=self.org, account=self.account, tab="failed",
        ).exists())

    def test_other_organization_is_excluded(self):
        other = Organization.objects.create(name="Other Inbox")
        account = WhatsAppAccount.objects.create(
            organization=other, phone_number_id="other-inbox",
        )
        pipeline = Pipeline.objects.create(organization=other, name="Sales")
        stage = Stage.objects.create(pipeline=pipeline, name="New")
        lead = Lead.objects.create(
            organization=other, pipeline=pipeline, stage=stage,
            name="Private lead", phone="+919000000006",
        )
        self.make_message(lead, organization=other, account=account)
        self.assertFalse(list_conversations(organization=self.org, tab="all").exists())
        self.assertFalse(list_conversations(
            organization=self.org, account=account, tab="all",
        ).exists())
