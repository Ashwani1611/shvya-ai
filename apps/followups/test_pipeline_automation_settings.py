from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Lead, Pipeline, Stage
from apps.followups.models import (
    AutoFollowupSettings,
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
)
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import (
    get_session_settings,
    update_session_settings,
)
from services.followup_service import (
    assign_sequence,
    process_due_state,
    register_lead_reply,
)


class PipelineAutomationSettingsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Pipeline Automation Org",
            timezone="Asia/Kolkata",
        )
        self.user = User.objects.create_user(
            email="pipeline-automation@example.com",
            organization=self.organization,
            password="test-password",
            name="Pipeline Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline_a = Pipeline.objects.create(
            organization=self.organization,
            name="Pipeline A",
            phone_number="+919999999991",
            owner=self.user,
            ai_enabled=True,
        )
        self.pipeline_b = Pipeline.objects.create(
            organization=self.organization,
            name="Pipeline B",
            phone_number="+919999999992",
            owner=self.user,
            ai_enabled=True,
        )
        self.stage_a = Stage.objects.create(
            pipeline=self.pipeline_a,
            name="New Lead",
            display_order=1,
        )
        self.stage_b = Stage.objects.create(
            pipeline=self.pipeline_b,
            name="New Lead",
            display_order=1,
        )
        self.account_a = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="API A",
            display_phone_number="+919999999991",
            phone_number_id="api-a",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.account_b = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="API B",
            display_phone_number="+919999999992",
            phone_number_id="api-b",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        AutoFollowupSettings.objects.create(
            organization=self.organization,
            enabled=False,
        )

    def test_ai_and_followup_settings_are_isolated_by_linked_pipeline(self):
        update_session_settings(
            account=self.account_a,
            payload={
                "ai_auto_reply": False,
                "auto_follow_up": False,
                "active_conversation_delay_value": 5,
                "active_conversation_delay_unit": "minutes",
            },
        )

        self.pipeline_a.refresh_from_db()
        self.pipeline_b.refresh_from_db()
        self.assertFalse(self.pipeline_a.ai_enabled)
        self.assertTrue(self.pipeline_b.ai_enabled)

        settings_a = get_session_settings(account=self.account_a)
        settings_b = get_session_settings(account=self.account_b)
        self.assertFalse(settings_a["ai_auto_reply"])
        self.assertFalse(settings_a["auto_follow_up"])
        self.assertTrue(settings_b["ai_auto_reply"])
        self.assertTrue(settings_b["auto_follow_up"])

        scheduler_master = AutoFollowupSettings.objects.get(
            organization=self.organization
        )
        self.assertTrue(scheduler_master.enabled)

    def test_knowledge_base_pipeline_ai_state_is_reflected_in_gear(self):
        self.pipeline_a.ai_enabled = False
        self.pipeline_a.save(update_fields=["ai_enabled", "updated_at"])

        settings = get_session_settings(account=self.account_a)

        self.assertFalse(settings["ai_auto_reply"])

    def test_api_dispatch_honors_pipeline_auto_followup_toggle(self):
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Pipeline A Sequence",
            whatsapp_account=self.account_a,
            created_by=self.user,
        )
        FollowupStep.objects.create(
            sequence=sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline_a,
            stage=self.stage_a,
            name="Lead A",
            phone="+919111111111",
        )
        update_session_settings(
            account=self.account_a,
            payload={"auto_follow_up": False},
        )
        state = assign_sequence(lead=lead, sequence=sequence, actor=self.user)
        state.upcoming_send_at = timezone.now() - timedelta(seconds=1)
        state.save(update_fields=["upcoming_send_at", "updated_at"])

        self.assertFalse(process_due_state(state.id))
        self.assertFalse(FollowupExecution.objects.filter(state=state).exists())

        update_session_settings(
            account=self.account_a,
            payload={"auto_follow_up": True},
        )
        state.refresh_from_db()
        state.upcoming_send_at = timezone.now() - timedelta(seconds=1)
        state.save(update_fields=["upcoming_send_at", "updated_at"])

        self.assertTrue(process_due_state(state.id))
        state.refresh_from_db()
        self.assertEqual(state.status, state.Status.COMPLETED)

    def test_conversation_delay_uses_linked_pipeline_number_settings(self):
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Delay Sequence",
            whatsapp_account=self.account_a,
            created_by=self.user,
        )
        FollowupStep.objects.create(
            sequence=sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline_a,
            stage=self.stage_a,
            name="Lead Delay",
            phone="+919222222222",
        )
        update_session_settings(
            account=self.account_a,
            payload={
                "active_conversation_delay_value": 5,
                "active_conversation_delay_unit": "minutes",
            },
        )
        state = assign_sequence(lead=lead, sequence=sequence, actor=self.user)
        replied_at = timezone.now()

        register_lead_reply(lead=lead, at=replied_at)
        state.refresh_from_db()

        self.assertEqual(state.paused_until, replied_at + timedelta(minutes=5))
