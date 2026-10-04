from datetime import timedelta
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.channels.instagram_models import (
    InstagramAccount,
    InstagramConversation,
    InstagramMessage,
)
from apps.channels.services.instagram_automation import (
    get_settings,
    update_settings,
    outbound_allowed,
)
from apps.followups.instagram import add_step, reschedule
from apps.followups.models import FollowupExecution
from apps.organizations.models import Organization
from services.channels.instagram_leads import ensure_instagram_lead
from services.followup_service import (
    create_sequence,
    assign_sequence,
    process_due_state,
    FollowupError,
)


class InstagramAutomationSettingsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Instagram Automation", package="dfy"
        )
        self.user = User.objects.create_user(
            email="ig-automation@example.com",
            password="test",
            name="Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key
        self.account = InstagramAccount.objects.create(
            organization=self.org,
            ig_user_id="ig-settings",
            access_token="test-token",
            status="connected",
        )
        self.conversation = InstagramConversation.objects.create(
            organization=self.org, account=self.account, participant_id="customer"
        )
        self.url = reverse("crm-instagram-automation-settings")

    def controls(self, **payload):
        return update_settings(organization_id=self.org.pk, payload=payload)

    def lead(self):
        _, lead = ensure_instagram_lead(conversation_id=self.conversation.pk)
        self.conversation.refresh_from_db()
        return lead

    def sequence(self):
        sequence = create_sequence(
            organization=self.org,
            created_by=self.user,
            name="Instagram follow-up",
            description="",
            provider="instagram",
        )
        add_step(sequence=sequence, body="Hi {lead_name}", schedule_type="immediate")
        return sequence

    def inbound(self):
        return InstagramMessage.objects.create(
            organization=self.org,
            account=self.account,
            conversation=self.conversation,
            direction="inbound",
            status="received",
            sender_id="customer",
            recipient_id="ig-settings",
            body="Hi",
            sent_at=timezone.now() - timedelta(hours=3),
        )

    def test_save_is_independent_and_roundtrips(self):
        original = {
            "hosted_whatsapp": {"sessions": {"wa": {"auto_follow_up": True}}},
            "other": {"keep": True},
        }
        self.org.settings = original
        self.org.save(update_fields=["settings"])
        response = self.client.post(
            self.url,
            {
                "ai_auto_reply": False,
                "auto_follow_up": True,
                "business_hours_start": "20:00",
                "business_hours_end": "06:00",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get(self.url).json()["settings"]["ai_auto_reply"])
        self.org.refresh_from_db()
        self.assertEqual(
            self.org.settings["hosted_whatsapp"], original["hosted_whatsapp"]
        )
        self.assertEqual(self.org.settings["other"], original["other"])

    def test_rejects_non_admin_and_malformed_input_without_writes(self):
        for payload in (
            [],
            {"bump_up_count": 11},
            {"business_hours_start": "25:00"},
            {"active_conversation_delay_unit": "weeks"},
        ):
            self.assertEqual(
                self.client.post(
                    self.url, payload, content_type="application/json"
                ).status_code,
                400,
            )
        self.user.role = User.Role.AGENT
        self.user.save(update_fields=["role"])
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.post(self.url, {}, content_type="application/json").status_code,
            403,
        )

    def test_auto_lead_creation_off_preserves_inbox_and_existing_links(self):
        self.controls(auto_lead_creation=False)
        _, lead = ensure_instagram_lead(conversation_id=self.conversation.pk)
        self.assertIsNone(lead)
        self.assertTrue(
            InstagramConversation.objects.filter(pk=self.conversation.pk).exists()
        )
        self.controls(auto_lead_creation=True)
        lead = self.lead()
        self.controls(auto_lead_creation=False)
        _, existing = ensure_instagram_lead(conversation_id=self.conversation.pk)
        self.assertEqual(existing.pk, lead.pk)

    def test_ai_switch_blocks_queued_ai_and_does_not_change_pipeline(self):
        lead = self.lead()
        lead.pipeline.ai_enabled = True
        lead.pipeline.save(update_fields=["ai_enabled"])
        lead.stage.ai_on = True
        lead.stage.save(update_fields=["ai_on"])
        lead.ai_enabled = True
        lead.save(update_fields=["ai_enabled"])
        from apps.ai_engagement.services.org_info import OrgInfoService

        info = OrgInfoService().get_or_create(organization=self.org)
        info.ai_enabled = True
        info.save(update_fields=["ai_enabled"])
        self.controls(ai_auto_reply=False)
        self.assertEqual(
            AIPermissionService()
            .evaluate(organization=self.org, lead=lead, channel="instagram")
            .reason,
            "instagram_ai_disabled",
        )
        message = InstagramMessage.objects.create(
            organization=self.org,
            account=self.account,
            conversation=self.conversation,
            direction="outbound",
            body="AI",
            raw_payload={"shvya_ai": {"origin": "reply"}},
        )
        self.assertFalse(outbound_allowed(message))
        lead.pipeline.refresh_from_db()
        self.assertTrue(lead.pipeline.ai_enabled)

    def test_phone_optional_sequence_delivery_is_durable_and_stops_when_disabled(self):
        lead = self.lead()
        self.inbound()
        self.controls(
            auto_follow_up=True,
            business_hours_start="00:00",
            business_hours_end="00:00",
            active_conversation_delay_value=1,
            active_conversation_delay_unit="minutes",
        )
        sequence = self.sequence()
        state = assign_sequence(lead=lead, sequence=sequence, actor=self.user)
        self.assertFalse(lead.phone)
        with patch("apps.channels.instagram_tasks.send_instagram_message_task.delay"):
            process_due_state(state.pk)
            process_due_state(state.pk)
        execution = FollowupExecution.objects.get(state=state)
        self.assertEqual(execution.status, "processing")
        self.assertEqual(
            InstagramMessage.objects.filter(
                raw_payload__has_key="shvya_followup"
            ).count(),
            1,
        )
        message = InstagramMessage.objects.get(
            pk=execution.payload["instagram_message_id"]
        )
        self.assertTrue(outbound_allowed(message))
        self.controls(auto_follow_up=False)
        self.assertFalse(outbound_allowed(message))
        self.controls(auto_follow_up=True)
        message.status = "sent"
        message.sent_at = timezone.now()
        message.save(update_fields=["status", "sent_at"])
        process_due_state(state.pk)
        execution.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(execution.status, "sent")
        self.assertEqual(state.status, "completed")

    def test_new_activity_delays_queued_followup(self):
        lead = self.lead()
        self.inbound()
        self.controls(
            auto_follow_up=True,
            business_hours_start="00:00",
            business_hours_end="00:00",
            active_conversation_delay_value=1,
            active_conversation_delay_unit="minutes",
        )
        state = assign_sequence(lead=lead, sequence=self.sequence())
        with patch("apps.channels.instagram_tasks.send_instagram_message_task.delay"):
            process_due_state(state.pk)
        execution = FollowupExecution.objects.get(state=state)
        message = InstagramMessage.objects.get(
            pk=execution.payload["instagram_message_id"]
        )
        inbound = self.inbound()
        inbound.sent_at = timezone.now()
        inbound.save(update_fields=["sent_at"])
        self.assertFalse(outbound_allowed(message))
        reschedule(organization_id=self.org.pk)
        state.refresh_from_db()
        self.assertGreater(state.upcoming_send_at, timezone.now())

    def test_other_organization_cannot_assign_sequence(self):
        lead = self.lead()
        other = Organization.objects.create(name="Other")
        InstagramAccount.objects.create(
            organization=other, ig_user_id="other-ig", status="connected"
        )
        foreign = create_sequence(
            organization=other,
            created_by=None,
            name="Foreign",
            description="",
            provider="instagram",
        )
        with self.assertRaises(FollowupError):
            assign_sequence(lead=lead, sequence=foreign)
        self.controls(ai_auto_reply=False)
        self.assertTrue(get_settings(organization_id=other.pk)["ai_auto_reply"])

    def test_create_edit_and_duplicate_instagram_sequence_pages(self):
        sequence = self.sequence()
        response = self.client.get(
            reverse("followups-sequence-edit", args=[sequence.pk])
        )
        self.assertContains(response, "Add Instagram")
        self.assertNotContains(response, "Add WhatsApp API")
        response = self.client.post(
            reverse("followups-sequence-duplicate", args=[sequence.pk])
        )
        self.assertEqual(response.status_code, 302)

    def test_bumpup_is_generated_once_and_blocked_after_customer_reply(self):
        from types import SimpleNamespace
        from apps.channels.services.instagram_automation import dispatch_bumps
        from apps.ai_engagement.services.org_info import OrgInfoService

        lead = self.lead()
        lead.ai_enabled = True
        lead.save(update_fields=["ai_enabled"])
        lead.pipeline.ai_enabled = True
        lead.pipeline.save(update_fields=["ai_enabled"])
        lead.stage.ai_on = True
        lead.stage.save(update_fields=["ai_on"])
        info = OrgInfoService().get_or_create(organization=self.org)
        info.ai_enabled = True
        info.bump_up_enabled = True
        info.bump_up_count = 2
        info.save()
        inbound = self.inbound()
        InstagramMessage.objects.filter(pk=inbound.pk).update(
            created_at=timezone.now() - timedelta(hours=3)
        )
        reply = InstagramMessage.objects.create(
            organization=self.org,
            account=self.account,
            conversation=self.conversation,
            direction="outbound",
            status="sent",
            body="How can I help?",
        )
        InstagramMessage.objects.filter(pk=reply.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        self.conversation.last_message_at = timezone.now() - timedelta(hours=2)
        self.conversation.save(update_fields=["last_message_at"])
        self.controls(bump_up_messages=True, bump_up_count=1)
        with (
            patch(
                "apps.ai_engagement.services.engagement.EngagementService"
            ) as service,
            patch("apps.channels.instagram_tasks.send_instagram_message_task.delay"),
        ):
            service.return_value._normalize_result.return_value = SimpleNamespace(
                should_engage=True, message="Can I help further?", model="test"
            )
            self.assertEqual(dispatch_bumps()["queued"], 1)
            self.assertEqual(dispatch_bumps()["queued"], 0)
        bump = InstagramMessage.objects.get(raw_payload__shvya_ai__origin="bump_up")
        self.assertTrue(outbound_allowed(bump))
        self.inbound()
        self.assertFalse(outbound_allowed(bump))

    def test_closed_instagram_window_never_queues_a_followup(self):
        lead = self.lead()
        inbound = self.inbound()
        inbound.sent_at = timezone.now() - timedelta(hours=25)
        inbound.save(update_fields=["sent_at"])
        self.controls(
            auto_follow_up=True,
            business_hours_start="00:00",
            business_hours_end="00:00",
        )
        state = assign_sequence(lead=lead, sequence=self.sequence())
        process_due_state(state.pk)
        self.assertFalse(InstagramMessage.objects.filter(direction="outbound").exists())
        state.refresh_from_db()
        self.assertEqual(state.status, "paused")
