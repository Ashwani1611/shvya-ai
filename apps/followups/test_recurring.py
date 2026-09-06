from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Lead, LeadReminder, Pipeline, Stage
from apps.followups.models import (
    AutoFollowupSettings,
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from apps.organizations.models import Organization
from services.followup_service import (
    _render_text,
    assign_sequence,
    calculate_step_due,
    dispatch_one_due_state,
)


class RecurringFollowupTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Recurring Test Org",
            timezone="Asia/Kolkata",
        )
        self.user = User.objects.create_user(
            email="recurring-admin@example.com",
            organization=self.organization,
            password="test-password",
            name="Recurring Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            phone_number="+919999999999",
            owner=self.user,
        )
        self.stage = Stage.objects.get(
            pipeline=self.pipeline,
            display_order=1,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Recurring Sender",
            display_phone_number="+919999999999",
            phone_number_id="123456789",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.sequence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Recurring Sequence",
            whatsapp_account=self.account,
            created_by=self.user,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Asha Mehta",
            phone="+919111111111",
            email="asha@example.com",
            attributes={"company_name": "Acme", "city": "Delhi"},
        )
        AutoFollowupSettings.objects.create(
            organization=self.organization,
            enabled=True,
            business_hours_start=time(0, 1),
            business_hours_end=time(23, 59),
            conversation_delay_value=2,
            conversation_delay_unit=AutoFollowupSettings.DelayUnit.HOURS,
        )

    def test_recurring_interval_calculates_from_previous_run(self):
        step = FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.RECURRING,
            recurring_every=2,
            recurring_unit=FollowupStep.DelayUnit.HOURS,
        )
        reference = datetime(2026, 9, 6, 10, 0, tzinfo=datetime_timezone.utc)

        due = calculate_step_due(
            step=step,
            reference=reference,
            organization=self.organization,
        )

        self.assertEqual(due, reference + timedelta(hours=2))

    def test_recurring_specific_days_use_organization_timezone(self):
        step = FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.RECURRING,
            recurring_weekdays=[
                FollowupStep.Weekday.MONDAY,
                FollowupStep.Weekday.WEDNESDAY,
            ],
            specific_time=time(9, 30),
        )
        reference = datetime(2026, 9, 6, 10, 0, tzinfo=datetime_timezone.utc)

        due = calculate_step_due(
            step=step,
            reference=reference,
            organization=self.organization,
        )

        self.assertEqual(
            due,
            datetime(2026, 9, 7, 4, 0, tzinfo=datetime_timezone.utc),
        )

    def test_recurring_reminder_stays_on_same_step_after_execution(self):
        step = FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call {{lead_first_name}} from {{company_name}}",
            schedule_type=FollowupStep.ScheduleType.RECURRING,
            recurring_every=2,
            recurring_unit=FollowupStep.DelayUnit.HOURS,
        )
        state = assign_sequence(
            lead=self.lead,
            sequence=self.sequence,
            actor=self.user,
        )
        LeadSequenceState.objects.filter(id=state.id).update(
            upcoming_send_at=timezone.now() - timedelta(seconds=1)
        )

        result = dispatch_one_due_state()

        self.assertEqual(result["status"], "processed")
        state.refresh_from_db()
        self.assertEqual(state.status, LeadSequenceState.Status.ACTIVE)
        self.assertEqual(state.next_step_id, step.id)
        self.assertEqual(state.last_completed_position, 0)
        self.assertGreater(state.upcoming_send_at, timezone.now())
        self.assertEqual(
            FollowupExecution.objects.filter(
                state=state,
                step=step,
                status=FollowupExecution.Status.CREATED,
            ).count(),
            1,
        )
        reminder = LeadReminder.objects.get(lead=self.lead, status="pending")
        self.assertEqual(reminder.description, "Call Asha from Acme")

    def test_personalization_uses_core_and_real_attribute_values(self):
        rendered = _render_text(
            "Hi {{lead_name}}, {{phone}} · {{email}} · {{org_name}} · "
            "{{company_name}} · {{city}}",
            self.lead,
            user=self.user,
        )

        self.assertEqual(
            rendered,
            "Hi Asha Mehta, +919111111111 · asha@example.com · "
            "Recurring Test Org · Acme · Delhi",
        )
