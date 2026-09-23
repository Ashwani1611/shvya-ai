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
        self.org = Organization.objects.create(package="dfy", name="Contact panel tests")
        self.other = Organization.objects.create(package="dfy", name="Other tenant")
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


    def sequence(self, account=None, name="Sequence"):
        sequence = FollowupSequence.objects.create(organization=self.org, whatsapp_account=account or self.account, name=name)
        FollowupStep.objects.create(sequence=sequence, position=1, step_type="reminder", reminder_text="Call customer")
        return sequence

    def test_disabled_lead_rejects_new_assignments_even_with_stale_instance(self):
        from services.followup_service import assign_sequence, FollowupError
        sequence = self.sequence()
        url = reverse("chat-followups-toggle", args=[self.lead.pk])
        self.assertEqual(self.client.post(url, {"enabled": "false"}).status_code, 200)
        with self.assertRaises(FollowupError):
            assign_sequence(lead=self.lead, sequence=sequence)
        self.assertEqual(self.client.post(reverse("chat-checking-in", args=[self.lead.pk]), {"sequence": sequence.pk}).status_code, 400)
        self.assertFalse(LeadSequenceState.objects.filter(lead=self.lead).exists())
        self.client.post(url, {"enabled": "true"})
        self.assertEqual(self.client.post(reverse("chat-checking-in", args=[self.lead.pk]), {"sequence": sequence.pk}).status_code, 200)

    def test_disabled_lead_is_checked_at_api_and_hosted_execution(self):
        from services.followup_service import assign_sequence, process_due_state, set_lead_followup_enabled, live_followup_due
        from services.channels.hosted_automation_service import process_hosted_due_state
        from django.utils import timezone
        state = assign_sequence(lead=self.lead, sequence=self.sequence())
        set_lead_followup_enabled(lead=self.lead, enabled=False)
        # Even a stale assignment flag must not bypass the persisted lead switch.
        LeadSequenceState.objects.filter(pk=state.pk).update(lead_auto_followup_enabled=True, upcoming_send_at=timezone.now())
        state.refresh_from_db()
        self.assertGreater(live_followup_due(state), timezone.now())
        with patch("services.followup_service._create_execution") as execution:
            self.assertFalse(process_due_state(state.pk))
            execution.assert_not_called()
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type"])
        with patch("services.followup_service._create_execution") as execution:
            self.assertFalse(process_hosted_due_state(state.pk))
            execution.assert_not_called()

    def test_hosted_contact_panel_replaces_templates_with_touchpoints(self):
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type"])
        response = self.client.get(
            self.panel,
            {"channel": "hosted", "account": self.account.pk},
        )
        self.assertNotContains(response, 'data-contact-tab="templates"')
        self.assertContains(response, 'data-contact-tab="touchpoints"')
        self.assertContains(response, 'data-contact-panel="touchpoints"')

    def test_hosted_picker_and_start_only_use_current_hosted_account(self):
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type"])
        self.unlinked.connection_type = "hosted"
        self.unlinked.save(update_fields=["connection_type"])
        selected = self.sequence(name="Correct hosted sequence")
        other = self.sequence(self.unlinked, "Wrong hosted sequence")
        response = self.client.get(self.panel, {"channel": "hosted", "account": self.account.pk})
        self.assertContains(response, "Correct hosted sequence")
        self.assertNotContains(response, "Wrong hosted sequence")
        url = reverse("chat-checking-in", args=[self.lead.pk])
        self.assertEqual(self.client.post(url, {"channel": "hosted", "account": self.account.pk, "sequence": selected.pk}).status_code, 200)
        self.assertEqual(self.client.post(url, {"channel": "hosted", "account": self.account.pk, "sequence": other.pk}).status_code, 404)

    def message(self, phone="+919123456789", **kwargs):
        from apps.channels.models import WhatsAppMessage
        return WhatsAppMessage.objects.create(organization=self.org, account=self.account, direction="inbound", from_number=phone,
                                              to_number="+919000000000", body="Hello", **kwargs)

    def test_api_existing_crm_phone_is_not_offered_as_create_lead(self):
        # Historical provider rows can remain unlinked even though CRM already
        # owns this phone. Both +E.164 and digits-only forms must be recognized.
        for phone in (self.lead.phone, self.lead.phone.lstrip("+")):
            message = self.message(phone=phone)
            response = self.client.get(reverse("whatsapp-chats"))
            self.assertNotContains(response, f'href="{reverse("whatsapp-unlinked-chat", args=[self.account.pk, message.pk])}"')
            message.delete()

    def test_api_unlinked_chat_creates_and_links_lead_idempotently(self):
        message = self.message()
        self.assertContains(self.client.get(reverse("whatsapp-chats")), "Create lead")
        self.assertContains(self.client.get(reverse("whatsapp-unlinked-chat", args=[self.account.pk, message.pk])), "Create a lead")
        url = reverse("chat-unlinked-contact", args=[self.account.pk])
        self.assertContains(self.client.get(url, {"chat": message.pk}), "Create lead")
        data = {"chat": str(message.pk), "name": "New customer", "pipeline": str(self.pipeline.pk), "phone": "+919999999999"}
        with (
            patch("services.crm.lead_service._schedule_new_lead_welcome") as welcome,
            patch("services.channels.chat_lead_service._queue_created_lead_engagement") as engagement,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(self.client.post(url, data).status_code, 200)
            welcome.assert_not_called()
        engagement.assert_called_once()
        self.assertEqual(engagement.call_args.kwargs["source_message_id"], message.pk)
        message.refresh_from_db()
        self.assertEqual(message.lead.phone, "+919123456789")
        self.assertEqual(Lead.objects.filter(organization=self.org, phone="+919123456789").count(), 1)

    def test_api_create_lead_persists_exact_inbound_as_durable_ai_turn(self):
        message = self.message()
        url = reverse("chat-unlinked-contact", args=[self.account.pk])
        data = {
            "chat": str(message.pk),
            "name": "AI activation customer",
            "pipeline": str(self.pipeline.pk),
        }

        with (
            patch(
                "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
            ) as publish,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200, response.content)
        message.refresh_from_db()
        self.assertIsNotNone(message.lead_id)
        execution = (message.raw_payload or {}).get("shvya_ai_execution") or {}
        self.assertEqual(execution.get("status"), "queued")
        publish.assert_called_once_with(
            args=[str(message.lead_id)],
            countdown=0,
        )

    def test_hosted_create_lead_activates_synced_history_once(self):
        from apps.hosted_automation.models import HostedAutomationJob
        from services.channels.hosted_automation_service import (
            EXPLICIT_LEAD_CREATION_AI_ACTIVATION,
        )

        self.pipeline.country_code = "+91"
        self.pipeline.phone_number = "9000000000"
        self.pipeline.ai_enabled = True
        self.pipeline.save(
            update_fields=["country_code", "phone_number", "ai_enabled", "updated_at"]
        )
        self.account.connection_type = "hosted"
        self.account.phone_number_id = "+919000000000"
        self.account.display_phone_number = "+919000000000"
        self.account.save(
            update_fields=[
                "connection_type",
                "phone_number_id",
                "display_phone_number",
                "updated_at",
            ]
        )
        message = self.message(
            raw_payload={
                "isHistory": True,
                "peerPhone": "+919123456789",
                "peerKey": "+919123456789",
                "contactName": "History customer",
            }
        )
        url = reverse("chat-unlinked-contact", args=[self.account.pk])
        data = {
            "chat": "+919123456789",
            "name": "History customer",
            "pipeline": str(self.pipeline.pk),
        }

        with (
            patch("apps.hosted_automation.signals.dispatch_due_hosted_ai.apply_async"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200, response.content)
        message.refresh_from_db()
        self.assertIsNotNone(message.lead_id)
        job = HostedAutomationJob.objects.get(source_message=message)
        self.assertEqual(job.status, HostedAutomationJob.Status.QUEUED)
        self.assertEqual(
            (job.result or {}).get("activation"),
            EXPLICIT_LEAD_CREATION_AI_ACTIVATION,
        )

        # Repeating Create Lead cannot create a second source-bound AI job.
        with self.captureOnCommitCallbacks(execute=True):
            retry = self.client.post(url, data)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(
            HostedAutomationJob.objects.filter(source_message=message).count(),
            1,
        )

    def test_existing_matching_lead_link_still_queues_ai_for_newly_attached_inbound(self):
        existing = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Already in CRM",
            phone="+919123456789",
            lead_source="system",
        )
        message = self.message(phone=existing.phone)
        url = reverse("chat-unlinked-contact", args=[self.account.pk])

        with (
            patch("services.channels.chat_lead_service._queue_created_lead_engagement") as engagement,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                url,
                {
                    "chat": str(message.pk),
                    "name": "Ignored duplicate name",
                    "pipeline": str(self.pipeline.pk),
                },
            )

        self.assertEqual(response.status_code, 200, response.content)
        message.refresh_from_db()
        self.assertEqual(message.lead_id, existing.pk)
        engagement.assert_called_once()
        self.assertEqual(engagement.call_args.kwargs["lead_id"], existing.pk)
        self.assertEqual(engagement.call_args.kwargs["source_message_id"], message.pk)

        # A retry after the history is already linked must not enqueue again.
        with (
            patch("services.channels.chat_lead_service._queue_created_lead_engagement") as retry_engagement,
            self.captureOnCommitCallbacks(execute=True),
        ):
            retry = self.client.post(
                url,
                {
                    "chat": str(message.pk),
                    "name": "Ignored duplicate name",
                    "pipeline": str(self.pipeline.pk),
                },
            )
        self.assertEqual(retry.status_code, 200, retry.content)
        retry_engagement.assert_not_called()

    def test_create_lead_rejects_wrong_pipeline_and_foreign_account(self):
        message = self.message()
        wrong = Pipeline.objects.create(organization=self.org, name="Wrong sender")
        url = reverse("chat-unlinked-contact", args=[self.account.pk])
        self.assertEqual(self.client.post(url, {"chat": str(message.pk), "name": "Test", "pipeline": str(wrong.pk)}).status_code, 400)
        self.account.organization = self.other
        self.account.save(update_fields=["organization"])
        self.assertEqual(self.client.get(url, {"chat": message.pk}).status_code, 404)

    def test_hosted_unlinked_contact_creates_lead_but_group_cannot(self):
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type"])
        message = self.message(raw_payload={"peerPhone": "+919123456789", "peerKey": "+919123456789", "contactName": "New customer"})
        url = reverse("chat-unlinked-contact", args=[self.account.pk])
        data = {"chat": "+919123456789", "name": "New customer", "pipeline": str(self.pipeline.pk)}
        self.assertContains(self.client.get(url, {"chat": data["chat"]}), "Create lead")
        self.assertEqual(self.client.post(url, data).status_code, 200)
        message.refresh_from_db()
        self.assertIsNotNone(message.lead_id)
        self.message(phone="12345@g.us", raw_payload={"isGroup": True, "chatId": "12345@g.us", "peerKey": "12345@g.us"})
        self.assertEqual(self.client.post(url, {**data, "chat": "12345@g.us"}).status_code, 400)

    def test_followup_toggle_rejects_invalid_values_and_other_tenant(self):
        url = reverse("chat-followups-toggle", args=[self.lead.pk])
        self.assertEqual(self.client.post(url, {"enabled": "maybe"}).status_code, 400)
        Lead.objects.filter(pk=self.lead.pk).update(organization=self.other)
        self.assertEqual(self.client.post(url, {"enabled": "false"}).status_code, 404)


    def test_instagram_create_requires_confirmed_phone_and_links_without_duplicates(self):
        from apps.channels.instagram_models import InstagramAccount, InstagramConversation
        account = InstagramAccount.objects.create(organization=self.org, ig_user_id="contact-panel-ig", access_token="test", status="connected")
        conversation = InstagramConversation.objects.create(organization=self.org, account=account, participant_id="contact", participant_name="IG Contact")
        url = reverse("chat-instagram-contact", args=[conversation.pk])
        self.assertContains(self.client.get(url), "Create lead")
        data = {"phone": "+919111222333", "name": "IG Contact", "pipeline": str(self.pipeline.pk)}
        self.assertEqual(self.client.post(url, data).status_code, 400)
        data["confirmed"] = "yes"
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(self.client.post(url, data).status_code, 200)
        conversation.refresh_from_db()
        self.assertEqual(conversation.lead.phone, data["phone"])
        self.assertEqual(conversation.lead.lead_source, "instagram")
        self.assertEqual(Lead.objects.filter(organization=self.org, phone=data["phone"]).count(), 1)
