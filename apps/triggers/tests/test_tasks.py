import uuid
from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerExecution
from apps.triggers.tasks import process_smart_trigger_event
from services.triggers.publisher import queue_trigger_event


class SmartTriggerQueueTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Queue Trigger Org")
        self.user = User.objects.create_user(
            email="queue-trigger@example.com",
            organization=self.organization,
            password="test-password",
            name="Queue Admin",
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
            name="Queue Lead",
            phone="+919222222222",
        )

    @patch("apps.triggers.tasks.process_smart_trigger_event.delay")
    def test_publisher_does_not_enqueue_without_active_listener(self, delay_mock):
        event_id = queue_trigger_event(
            organization_id=self.organization.id,
            lead_id=self.lead.id,
            event_type=SmartTrigger.EventType.LEAD_UPDATED,
            payload={},
        )

        self.assertIsNone(event_id)
        delay_mock.assert_not_called()

    @patch("apps.triggers.tasks.process_smart_trigger_event.delay")
    def test_publisher_enqueues_when_active_listener_exists(self, delay_mock):
        SmartTrigger.objects.create(
            organization=self.organization,
            created_by=self.user,
            name="Queue listener",
            event_type=SmartTrigger.EventType.LEAD_UPDATED,
            actions=[{"type": "clear_sequence"}],
        )

        event_id = queue_trigger_event(
            organization_id=self.organization.id,
            lead_id=self.lead.id,
            event_type=SmartTrigger.EventType.LEAD_UPDATED,
            payload={"changed_fields": ["email"]},
        )

        self.assertIsNotNone(event_id)
        delay_mock.assert_called_once_with(
            event_id,
            str(self.organization.id),
            str(self.lead.id),
            SmartTrigger.EventType.LEAD_UPDATED,
            {"changed_fields": ["email"]},
        )

    def test_task_processes_event_through_service(self):
        trigger = SmartTrigger.objects.create(
            organization=self.organization,
            created_by=self.user,
            name="Task listener",
            event_type=SmartTrigger.EventType.LEAD_UPDATED,
            actions=[{"type": "add_note", "text": "Task ran"}],
        )
        event_id = str(uuid.uuid4())

        result = process_smart_trigger_event.run(
            event_id,
            str(self.organization.id),
            str(self.lead.id),
            SmartTrigger.EventType.LEAD_UPDATED,
            {},
        )

        self.assertEqual(result["evaluated"], 1)
        self.assertTrue(
            TriggerExecution.objects.filter(
                trigger=trigger,
                event_id=event_id,
                status=TriggerExecution.Status.SUCCESS,
            ).exists()
        )
