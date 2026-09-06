import uuid

from django.test import TestCase

from apps.accounts.models import User
from apps.crm.models import Lead, LeadNote, Pipeline, Stage
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerExecution
from services.triggers.evaluator import conditions_match, process_event


class SmartTriggerEvaluatorTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Smart Trigger Test Org",
            timezone="Asia/Kolkata",
        )
        self.user = User.objects.create_user(
            email="trigger-admin@example.com",
            organization=self.organization,
            password="test-password",
            name="Trigger Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Leads",
        )
        self.stage = Stage.objects.filter(pipeline=self.pipeline).order_by(
            "display_order"
        ).first()
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Asha Mehta",
            phone="+919111111111",
            email="asha@example.com",
            attributes={"city": "Delhi", "plan_interest": "Pro"},
        )

    def _trigger(self, **overrides):
        values = {
            "organization": self.organization,
            "created_by": self.user,
            "name": f"Trigger {uuid.uuid4()}",
            "event_type": SmartTrigger.EventType.LEAD_UPDATED,
            "condition_mode": SmartTrigger.ConditionMode.ALL,
            "conditions": [
                {
                    "field": "attr.city",
                    "operator": "equals",
                    "value": "delhi",
                }
            ],
            "actions": [
                {
                    "type": "add_note",
                    "text": "Matched {{lead_name}} in {{city}}",
                },
                {"type": "set_ai_enabled", "enabled": False},
            ],
            "is_active": True,
        }
        values.update(overrides)
        return SmartTrigger.objects.create(**values)

    def test_conditions_use_core_event_and_custom_lead_values(self):
        trigger = self._trigger(
            conditions=[
                {"field": "lead.name", "operator": "contains", "value": "asha"},
                {"field": "attr.plan_interest", "operator": "equals", "value": "pro"},
                {
                    "field": "event.changed_fields",
                    "operator": "contains",
                    "value": "email",
                },
            ]
        )

        self.assertTrue(
            conditions_match(
                trigger=trigger,
                lead=self.lead,
                payload={"changed_fields": ["email", "notes"]},
            )
        )

    def test_matching_event_executes_actions_and_writes_audit_log(self):
        trigger = self._trigger()
        event_id = uuid.uuid4()

        result = process_event(
            event_id=event_id,
            organization_id=self.organization.id,
            lead_id=self.lead.id,
            event_type=SmartTrigger.EventType.LEAD_UPDATED,
            payload={"changed_fields": ["email"]},
        )

        self.assertEqual(result["evaluated"], 1)
        execution = TriggerExecution.objects.get(trigger=trigger, event_id=event_id)
        self.assertEqual(execution.status, TriggerExecution.Status.SUCCESS)
        self.assertTrue(execution.matched)
        self.assertEqual(len(execution.action_results), 2)
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.ai_enabled)
        self.assertEqual(
            LeadNote.objects.get(lead=self.lead).note,
            "Matched Asha Mehta in Delhi",
        )
        trigger.refresh_from_db()
        self.assertEqual(trigger.successful_runs, 1)
        self.assertIsNotNone(trigger.last_fired_at)

    def test_non_matching_event_is_skipped_without_actions(self):
        trigger = self._trigger(
            conditions=[
                {"field": "lead.email", "operator": "is_empty"},
            ]
        )

        process_event(
            event_id=uuid.uuid4(),
            organization_id=self.organization.id,
            lead_id=self.lead.id,
            event_type=SmartTrigger.EventType.LEAD_UPDATED,
            payload={},
        )

        execution = TriggerExecution.objects.get(trigger=trigger)
        self.assertEqual(execution.status, TriggerExecution.Status.SKIPPED)
        self.assertEqual(execution.skip_reason, "Conditions did not match.")
        self.assertFalse(LeadNote.objects.filter(lead=self.lead).exists())

    def test_once_per_lead_blocks_later_successful_run(self):
        trigger = self._trigger(once_per_lead=True)

        for _ in range(2):
            process_event(
                event_id=uuid.uuid4(),
                organization_id=self.organization.id,
                lead_id=self.lead.id,
                event_type=SmartTrigger.EventType.LEAD_UPDATED,
                payload={},
            )

        executions = list(trigger.executions.order_by("created_at"))
        self.assertEqual(executions[0].status, TriggerExecution.Status.SUCCESS)
        self.assertEqual(executions[1].status, TriggerExecution.Status.SKIPPED)
        self.assertIn("only once per lead", executions[1].skip_reason)
        self.assertEqual(LeadNote.objects.filter(lead=self.lead).count(), 1)

    def test_same_event_id_is_idempotent_per_trigger(self):
        trigger = self._trigger()
        event_id = uuid.uuid4()

        for _ in range(2):
            process_event(
                event_id=event_id,
                organization_id=self.organization.id,
                lead_id=self.lead.id,
                event_type=SmartTrigger.EventType.LEAD_UPDATED,
                payload={},
            )

        self.assertEqual(
            TriggerExecution.objects.filter(trigger=trigger, event_id=event_id).count(),
            1,
        )
        self.assertEqual(LeadNote.objects.filter(lead=self.lead).count(), 1)
