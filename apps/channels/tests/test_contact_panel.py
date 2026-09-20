"""HTTP boundaries for the shared contact panel and Touchpoints library."""

from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.models import Lead, Pipeline, Stage
from apps.followups.models import (
    TouchpointCategory,
    TouchpointReply,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from apps.organizations.models import Organization


class ContactPanelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Contact panel tests")
        self.other = Organization.objects.create(name="Other tenant")
        self.user = User.objects.create_user(
            email="contact-panel@example.com",
            name="Admin",
            organization=self.org,
            role=User.Role.ADMIN,
            password="test",
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key
        self.pipeline = Pipeline.objects.create(
            organization=self.org, name="Sales", phone_number="linked-number"
        )
        self.stage = Stage.objects.create(pipeline=self.pipeline, name="New")
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Customer",
            phone="+919876543210",
            lead_source="whatsapp_api",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            phone_number_id="linked-number",
            connection_type="api",
            status="connected",
            is_active=True,
        )
        self.unlinked = WhatsAppAccount.objects.create(
            organization=self.org,
            phone_number_id="different-number",
            connection_type="api",
            status="connected",
            is_active=True,
        )
        self.manage = reverse("crm-auto-follow-ups-touchpoints")
        self.panel = reverse("chat-contact-panel", args=[self.lead.pk])

    def test_category_and_reply_crud_and_cascade(self):
        self.assertEqual(
            self.client.post(
                self.manage, {"action": "save_category", "name": "Welcome"}
            ).status_code,
            200,
        )
        category = TouchpointCategory.objects.get(organization=self.org)
        response = self.client.post(
            self.manage,
            {
                "action": "save_reply",
                "category_id": category.pk,
                "title": "Hello",
                "body": "How can we help?",
            },
        )
        self.assertEqual(response.status_code, 200)
        reply = category.replies.get()
        self.assertContains(self.client.get(self.panel), "How can we help?")
        self.assertEqual(
            self.client.post(
                self.manage,
                {
                    "action": "save_reply",
                    "reply_id": reply.pk,
                    "category_id": category.pk,
                    "title": "Greeting",
                    "body": "Welcome back",
                },
            ).status_code,
            200,
        )
        reply.refresh_from_db()
        self.assertEqual(reply.body, "Welcome back")
        self.assertEqual(
            self.client.post(
                self.manage,
                {
                    "action": "save_category",
                    "category_id": category.pk,
                    "name": "Greetings",
                },
            ).status_code,
            200,
        )
        category.refresh_from_db()
        self.assertEqual(category.name, "Greetings")
        self.assertEqual(
            self.client.post(
                self.manage, {"action": "delete_category", "category_id": category.pk}
            ).status_code,
            200,
        )
        self.assertFalse(TouchpointReply.objects.filter(pk=reply.pk).exists())

    def test_cross_tenant_replies_and_categories_cannot_be_read_or_mutated(self):
        category = TouchpointCategory.objects.create(
            organization=self.other, name="Private category"
        )
        reply = TouchpointReply.objects.create(
            category=category, title="Private reply", body="Private content"
        )
        self.assertNotContains(self.client.get(self.manage), "Private category")
        self.assertNotContains(self.client.get(self.panel), "Private content")
        for data in [
            {"action": "delete_category", "category_id": category.pk},
            {"action": "delete_reply", "reply_id": reply.pk},
            {
                "action": "save_reply",
                "category_id": category.pk,
                "title": "New",
                "body": "Message",
            },
        ]:
            self.assertEqual(self.client.post(self.manage, data).status_code, 404)
        self.assertTrue(TouchpointReply.objects.filter(pk=reply.pk).exists())

    def test_rejects_blank_duplicate_and_oversize_content(self):
        TouchpointCategory.objects.create(organization=self.org, name="Existing")
        for name in ["", "  ", "Existing", "x" * 101]:
            self.assertEqual(
                self.client.post(
                    self.manage, {"action": "save_category", "name": name}
                ).status_code,
                400,
            )
        category = TouchpointCategory.objects.get(organization=self.org)
        self.assertEqual(
            self.client.post(
                self.manage,
                {
                    "action": "save_reply",
                    "category_id": category.pk,
                    "title": "Reply",
                    "body": "x" * 1001,
                },
            ).status_code,
            400,
        )

    def test_templates_and_sequences_are_linked_sender_only(self):
        for account, title in [(self.account, "Linked"), (self.unlinked, "Unlinked")]:
            WhatsAppTemplate.objects.create(
                organization=self.org,
                account=account,
                name=title,
                body="Hello",
                status="approved",
                created_by=self.user,
            )
            FollowupSequence.objects.create(
                organization=self.org,
                whatsapp_account=account,
                name=title + " sequence",
            )
        response = self.client.get(self.panel)
        self.assertContains(response, "Linked sequence")
        self.assertNotContains(response, "Unlinked")
        self.assertNotContains(
            self.client.get(self.panel, {"account": self.unlinked.pk}),
            "Linked sequence",
        )
        self.assertNotContains(
            self.client.get(self.panel, {"channel": "instagram"}), "Linked sequence"
        )

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_forged_template_for_another_sender_is_rejected(self, delay):
        template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=self.unlinked,
            name="Wrong sender",
            body="Hello",
            status="approved",
            created_by=self.user,
        )
        response = self.client.post(
            reverse("whatsapp-send-template", args=[self.lead.pk]),
            {"template_id": template.pk},
        )
        self.assertEqual(response.status_code, 400)
        delay.assert_not_called()

    def test_phone_and_source_are_locked_while_details_autosave(self):
        response = self.client.post(
            reverse("whatsapp-lead-quick-update", args=[self.lead.pk]),
            {
                "name": "Updated",
                "email": "updated@example.com",
                "phone": "+919000000000",
                "lead_source": "system",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(
            (self.lead.name, self.lead.email), ("Updated", "updated@example.com")
        )
        self.assertEqual(
            (self.lead.phone, self.lead.lead_source), ("+919876543210", "whatsapp_api")
        )

    def test_stage_choices_and_mutations_cannot_cross_pipelines(self):
        pipeline = Pipeline.objects.create(organization=self.org, name="Other pipeline")
        stage = Stage.objects.create(pipeline=pipeline, name="Foreign stage")
        self.assertNotContains(self.client.get(self.panel), "Foreign stage")
        response = self.client.post(
            reverse("whatsapp-lead-quick-update", args=[self.lead.pk]),
            {"stage": stage.pk},
        )
        self.assertEqual(response.status_code, 400)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.stage.pk)

    def test_checking_in_assigns_real_sequence_state_and_rejects_other_sender(self):
        sequence = FollowupSequence.objects.create(
            organization=self.org, whatsapp_account=self.account, name="Check in"
        )
        FollowupStep.objects.create(
            sequence=sequence,
            position=1,
            step_type="reminder",
            reminder_text="Call customer",
        )
        url = reverse("chat-checking-in", args=[self.lead.pk])
        self.assertEqual(
            self.client.post(url, {"sequence": sequence.pk}).status_code, 200
        )
        state = LeadSequenceState.objects.get(lead=self.lead, sequence=sequence)
        self.assertEqual(state.status, "active")
        self.assertContains(self.client.get(self.panel), "Check in")
        wrong = FollowupSequence.objects.create(
            organization=self.org, whatsapp_account=self.unlinked, name="Other sequence"
        )
        self.assertEqual(self.client.post(url, {"sequence": wrong.pk}).status_code, 404)
        self.assertEqual(
            self.client.post(url, {"sequence": "invalid"}).status_code, 400
        )

    def test_other_tenant_cannot_access_contact_panel(self):
        pipeline = Pipeline.objects.create(organization=self.other, name="Other")
        stage = Stage.objects.create(pipeline=pipeline, name="New")
        lead = Lead.objects.create(
            organization=self.other,
            pipeline=pipeline,
            stage=stage,
            name="Secret",
            phone="+919999999999",
        )
        self.assertEqual(
            self.client.get(reverse("chat-contact-panel", args=[lead.pk])).status_code,
            404,
        )
