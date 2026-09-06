from datetime import datetime, time, timedelta
from datetime import timezone as datetime_timezone

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
    LeadSequenceState,
)
from apps.organizations.models import Organization
from services.followup_service import (
    assign_sequence,
    calculate_step_due,
    delete_sequence,
    dispatch_one_due_state,
    register_lead_reply,
)


class AutoFollowupServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Followup Test Org",
            timezone="Asia/Kolkata",
        )
        self.user = User.objects.create_user(
            email="admin-followups@example.com",
            organization=self.organization,
            password="test-password",
            name="Followup Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Leads",
            phone_number="+919999999999",
            owner=self.user,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            display_order=1,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Test Sender",
            display_phone_number="+919999999999",
            phone_number_id="123456789",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.sequence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Test Sequence",
            whatsapp_account=self.account,
            created_by=self.user,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Lead One",
            phone="+919111111111",
            email="lead1@example.com",
        )
        AutoFollowupSettings.objects.create(
            organization=self.organization,
            enabled=True,
            business_hours_start=time(0, 1),
            business_hours_end=time(23, 59),
            conversation_delay_value=2,
            conversation_delay_unit=AutoFollowupSettings.DelayUnit.HOURS,
        )

    def test_specific_time_uses_organization_timezone(self):
        step = FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.SPECIFIC_TIME,
            specific_time=time(9, 30),
        )
        reference = datetime(2026, 9, 6, 10, 0, tzinfo=datetime_timezone.utc)
        due = calculate_step_due(
            step=step,
            reference=reference,
            organization=self.organization,
        )
        # 10:00 UTC is 15:30 IST, so the next 09:30 IST occurrence is the
        # following day at 04:00 UTC.
        self.assertEqual(
            due,
            datetime(2026, 9, 7, 4, 0, tzinfo=datetime_timezone.utc),
        )

    def test_switching_back_to_sequence_resumes_after_last_completed_step(self):
        first = FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="First",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        second = FollowupStep.objects.create(
            sequence=self.sequence,
            position=2,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Second",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        other_sequence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Other Sequence",
            whatsapp_account=self.account,
            created_by=self.user,
        )
        FollowupStep.objects.create(
            sequence=other_sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Other",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )

        state = assign_sequence(lead=self.lead, sequence=self.sequence, actor=self.user)
        state.last_completed_position = first.position
        state.save(update_fields=["last_completed_position", "updated_at"])

        assign_sequence(lead=self.lead, sequence=other_sequence, actor=self.user)
        resumed = assign_sequence(lead=self.lead, sequence=self.sequence, actor=self.user)

        self.assertEqual(resumed.next_step_id, second.id)
        self.assertEqual(resumed.last_completed_position, 1)
        self.assertEqual(
            LeadSequenceState.objects.filter(
                lead=self.lead,
                status__in=[LeadSequenceState.Status.ACTIVE, LeadSequenceState.Status.PAUSED],
            ).count(),
            1,
        )

    def test_assignment_records_triggering_user(self):
        FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        state = assign_sequence(lead=self.lead, sequence=self.sequence, actor=self.user)
        self.assertEqual(state.assigned_by, self.user)

    def test_sequence_delete_removes_active_assignments_steps_and_history(self):
        step = FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        state = assign_sequence(lead=self.lead, sequence=self.sequence, actor=self.user)
        execution = FollowupExecution.objects.create(
            organization=self.organization,
            state=state,
            lead=self.lead,
            sequence=self.sequence,
            step=step,
            scheduled_for=timezone.now(),
        )
        sequence_id = self.sequence.id
        state_id = state.id
        step_id = step.id
        execution_id = execution.id

        delete_sequence(sequence=self.sequence)

        self.assertFalse(FollowupSequence.objects.filter(id=sequence_id).exists())
        self.assertFalse(LeadSequenceState.objects.filter(id=state_id).exists())
        self.assertFalse(FollowupStep.objects.filter(id=step_id).exists())
        self.assertFalse(FollowupExecution.objects.filter(id=execution_id).exists())

    def test_lead_reply_delays_next_send_without_clearing_sequence(self):
        FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        state = assign_sequence(lead=self.lead, sequence=self.sequence, actor=self.user)
        reply_at = timezone.now()
        register_lead_reply(lead=self.lead, at=reply_at)
        state.refresh_from_db()

        self.assertEqual(state.status, LeadSequenceState.Status.ACTIVE)
        self.assertEqual(state.last_inbound_at, reply_at)
        self.assertGreaterEqual(state.upcoming_send_at, reply_at + timedelta(hours=2))

    def test_dispatcher_processes_only_one_due_lead_per_pass(self):
        FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call lead",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        second_lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Lead Two",
            phone="+919222222222",
            email="lead2@example.com",
        )
        first_state = assign_sequence(lead=self.lead, sequence=self.sequence, actor=self.user)
        second_state = assign_sequence(lead=second_lead, sequence=self.sequence, actor=self.user)
        now = timezone.now() - timedelta(seconds=1)
        LeadSequenceState.objects.filter(id__in=[first_state.id, second_state.id]).update(
            upcoming_send_at=now
        )

        result = dispatch_one_due_state()

        self.assertEqual(result["status"], "processed")
        statuses = list(
            LeadSequenceState.objects.filter(id__in=[first_state.id, second_state.id])
            .values_list("status", flat=True)
        )
        self.assertEqual(statuses.count(LeadSequenceState.Status.COMPLETED), 1)
        self.assertEqual(statuses.count(LeadSequenceState.Status.ACTIVE), 1)
