from django.test import TestCase

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger
from services.triggers.evaluator import TriggerConfigurationError
from services.triggers.trigger_service import create_trigger, duplicate_trigger


class SmartTriggerModelServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Trigger Model Org")
        self.user = User.objects.create_user(
            email="trigger-model@example.com",
            organization=self.organization,
            password="test-password",
            name="Trigger Model Admin",
            role=User.Role.ADMIN,
        )

    def test_create_trigger_validates_and_persists_rule(self):
        trigger = create_trigger(
            organization=self.organization,
            created_by=self.user,
            name="New lead note",
            description="Add a system note for every new lead",
            event_type=SmartTrigger.EventType.LEAD_CREATED,
            condition_mode=SmartTrigger.ConditionMode.ALL,
            conditions=[],
            actions=[{"type": "add_note", "text": "Created automatically"}],
            is_active=True,
            once_per_lead=True,
            cooldown_minutes=0,
        )

        self.assertEqual(trigger.organization, self.organization)
        self.assertTrue(trigger.is_active)
        self.assertTrue(trigger.once_per_lead)
        self.assertEqual(trigger.actions[0]["type"], "add_note")

    def test_duplicate_is_paused_until_reviewed(self):
        trigger = create_trigger(
            organization=self.organization,
            created_by=self.user,
            name="Original",
            description="",
            event_type=SmartTrigger.EventType.LEAD_UPDATED,
            condition_mode=SmartTrigger.ConditionMode.ALL,
            conditions=[],
            actions=[{"type": "clear_sequence"}],
            is_active=True,
            once_per_lead=False,
            cooldown_minutes=30,
        )

        copied = duplicate_trigger(trigger=trigger, created_by=self.user)

        self.assertFalse(copied.is_active)
        self.assertEqual(copied.cooldown_minutes, 30)
        self.assertNotEqual(copied.name, trigger.name)
        self.assertEqual(copied.actions, trigger.actions)

    def test_invalid_action_is_rejected(self):
        with self.assertRaises(TriggerConfigurationError):
            create_trigger(
                organization=self.organization,
                created_by=self.user,
                name="Broken",
                description="",
                event_type=SmartTrigger.EventType.LEAD_CREATED,
                condition_mode=SmartTrigger.ConditionMode.ALL,
                conditions=[],
                actions=[{"type": "unknown_action"}],
                is_active=True,
                once_per_lead=False,
                cooldown_minutes=0,
            )
