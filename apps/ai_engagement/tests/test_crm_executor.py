from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_engagement.services.crm_executor import (
    CRMActionExecutionError,
    CRMActionExecutor,
)
from apps.crm.models import (
    AttributeDefinition,
    Lead,
    LeadContact,
    LeadNote,
    LeadReminder,
    Pipeline,
    Stage,
)
from apps.organizations.models import Organization


class CRMActionExecutorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Executor Test Organization",
        )

        cls.owner = User.objects.create_user(
            email="owner@example.com",
            password="password",
            name="Pipeline Owner",
            organization=cls.organization,
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Sales Pipeline",
            description="Test pipeline.",
            owner=cls.owner,
            is_active=True,
        )

        # Pipeline creation already creates the default stages in this project.
        # Reuse those stages instead of creating duplicate rows.
        cls.stages = list(
            Stage.objects.filter(
                pipeline=cls.pipeline,
                is_active=True,
            ).order_by("display_order")
        )

        if len(cls.stages) < 2:
            raise AssertionError(
                "Expected Pipeline creation to provide at least two active stages."
            )

        cls.stage_one = cls.stages[0]
        cls.stage_two = cls.stages[1]

        cls.other_organization = Organization.objects.create(
            name="Other Organization",
        )

    def setUp(self):
        self.lead = self._create_lead(
            phone="+919999999991",
            name="Test Lead",
            stage=self.stage_one,
        )

        self.executor = CRMActionExecutor()

    def _create_lead(self, *, phone, name, stage):
        return Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=stage,
            name=name,
            phone=phone,
            email="lead@example.com",
            notes="",
            attributes={},
        )

    # ============================================================
    # EMPTY
    # ============================================================

    def test_empty_actions_returns_empty_result(self):
        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[],
        )

        self.assertEqual(result, [])

    # ============================================================
    # ORGANIZATION / LEAD SCOPE
    # ============================================================

    def test_rejects_lead_from_other_organization(self):
        other_pipeline = Pipeline.objects.create(
            organization=self.other_organization,
            name="Other Pipeline",
            owner=None,
            is_active=True,
        )

        other_stage = Stage.objects.filter(
            pipeline=other_pipeline,
            is_active=True,
        ).order_by("display_order").first()

        self.assertIsNotNone(other_stage)

        other_lead = Lead.objects.create(
            organization=self.other_organization,
            pipeline=other_pipeline,
            stage=other_stage,
            name="Other Lead",
            phone="+919999999992",
            email="other@example.com",
            attributes={},
        )

        with self.assertRaises(CRMActionExecutionError):
            self.executor.execute(
                organization=self.organization,
                lead=other_lead,
                actions=[],
            )

    # ============================================================
    # ATTRIBUTE UPDATES
    # ============================================================

    def test_executes_attribute_update(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Budget",
            key="budget",
            field_type="text",
            description="Lead budget.",
            options=[],
            display_order=1,
        )

        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "attribute_updates",
                    "updates": [
                        {
                            "key": "budget",
                            "value": "50000",
                        }
                    ],
                }
            ],
        )

        self.lead.refresh_from_db()

        self.assertEqual(
            self.lead.attributes["budget"],
            "50000",
        )
        self.assertEqual(result[0]["type"], "attribute_updates")
        self.assertEqual(result[0]["status"], "executed")

    def test_rejects_unknown_attribute_key(self):
        with self.assertRaises(CRMActionExecutionError):
            self.executor.execute(
                organization=self.organization,
                lead=self.lead,
                actions=[
                    {
                        "type": "attribute_updates",
                        "updates": [
                            {
                                "key": "does_not_exist",
                                "value": "value",
                            }
                        ],
                    }
                ],
            )

    # ============================================================
    # STAGE TRANSITION
    # ============================================================

    def test_executes_stage_transition(self):
        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "pipeline_transition",
                    "stage_shift": {
                        "stage_id": str(self.stage_two.id),
                    },
                }
            ],
        )

        self.lead.refresh_from_db()

        self.assertEqual(
            self.lead.stage_id,
            self.stage_two.id,
        )
        self.assertIsNotNone(self.lead.stage_entered_at)
        self.assertEqual(result[0]["status"], "executed")

    def test_same_stage_is_no_op(self):
        original_stage_entered_at = self.lead.stage_entered_at

        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "pipeline_transition",
                    "stage_shift": {
                        "stage_id": str(self.stage_one.id),
                    },
                }
            ],
        )

        self.lead.refresh_from_db()

        self.assertEqual(
            self.lead.stage_id,
            self.stage_one.id,
        )
        self.assertEqual(
            self.lead.stage_entered_at,
            original_stage_entered_at,
        )
        self.assertEqual(result[0]["status"], "no_op")

    def test_rejects_stage_from_other_pipeline(self):
        other_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Other Pipeline",
            owner=self.owner,
            is_active=True,
        )

        other_stage = Stage.objects.filter(
            pipeline=other_pipeline,
            is_active=True,
        ).order_by("display_order").first()

        self.assertIsNotNone(other_stage)

        with self.assertRaises(CRMActionExecutionError):
            self.executor.execute(
                organization=self.organization,
                lead=self.lead,
                actions=[
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {
                            "stage_id": str(other_stage.id),
                        },
                    }
                ],
            )

    def test_rejects_inactive_stage(self):
        inactive_stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="Inactive Test Stage",
            display_order=999,
            is_active=False,
        )

        with self.assertRaises(CRMActionExecutionError):
            self.executor.execute(
                organization=self.organization,
                lead=self.lead,
                actions=[
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {
                            "stage_id": str(inactive_stage.id),
                        },
                    }
                ],
            )

    # ============================================================
    # NOTE
    # ============================================================

    def test_executes_add_note(self):
        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "add_note",
                    "note": "Lead confirmed interest.",
                }
            ],
        )

        note = LeadNote.objects.get(lead=self.lead)

        self.assertEqual(
            note.note,
            "Lead confirmed interest.",
        )
        self.assertEqual(note.note_type, "system")
        self.assertEqual(result[0]["type"], "add_note")
        self.assertEqual(result[0]["status"], "executed")

    def test_ai_note_can_have_no_actor(self):
        self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "add_note",
                    "note": "AI-generated CRM note.",
                }
            ],
            actor=None,
        )

        note = LeadNote.objects.get(lead=self.lead)

        self.assertIsNone(note.created_by)

    # ============================================================
    # REMINDER
    # ============================================================

    def test_executes_reminder_using_pipeline_owner(self):
        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "create_reminder",
                    "title": "Follow up",
                    "description": "Discuss enrollment.",
                    "due_at": "2026-09-05T10:00:00+05:30",
                }
            ],
        )

        reminder = LeadReminder.objects.get(lead=self.lead)

        self.assertEqual(reminder.title, "Follow up")
        self.assertEqual(reminder.description, "Discuss enrollment.")
        self.assertEqual(reminder.status, "pending")
        self.assertEqual(reminder.assigned_to_id, self.owner.id)
        self.assertEqual(result[0]["type"], "create_reminder")
        self.assertEqual(result[0]["status"], "executed")

    def test_naive_reminder_datetime_becomes_aware(self):
        self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "create_reminder",
                    "title": "Follow up",
                    "description": "",
                    "due_at": "2026-09-05T10:00:00",
                }
            ],
        )

        reminder = LeadReminder.objects.get(lead=self.lead)

        self.assertTrue(timezone.is_aware(reminder.due_at))

    # ============================================================
    # CONTACT
    # ============================================================

    def test_executes_contact_update(self):
        contact = LeadContact.objects.create(
            lead=self.lead,
            channel="whatsapp",
            handle="+919999999999",
            verified=False,
        )

        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "contact_updates",
                    "updates": [
                        {
                            "contact_id": str(contact.id),
                            "channel": "whatsapp",
                            "handle": "+918888888888",
                        }
                    ],
                }
            ],
        )

        contact.refresh_from_db()

        self.assertEqual(contact.handle, "+918888888888")
        self.assertEqual(contact.channel, "whatsapp")
        self.assertEqual(result[0]["type"], "contact_updates")
        self.assertEqual(result[0]["status"], "executed")

    def test_rejects_contact_from_other_lead(self):
        other_lead = self._create_lead(
            phone="+919999999993",
            name="Other Lead",
            stage=self.stage_one,
        )

        contact = LeadContact.objects.create(
            lead=other_lead,
            channel="whatsapp",
            handle="+917777777777",
            verified=False,
        )

        with self.assertRaises(CRMActionExecutionError):
            self.executor.execute(
                organization=self.organization,
                lead=self.lead,
                actions=[
                    {
                        "type": "contact_updates",
                        "updates": [
                            {
                                "contact_id": str(contact.id),
                                "channel": "whatsapp",
                                "handle": "+916666666666",
                            }
                        ],
                    }
                ],
            )

    # ============================================================
    # TRANSACTION ROLLBACK
    # ============================================================

    def test_all_actions_roll_back_when_later_action_fails(self):
        initial_attributes = dict(self.lead.attributes or {})

        with self.assertRaises(CRMActionExecutionError):
            self.executor.execute(
                organization=self.organization,
                lead=self.lead,
                actions=[
                    {
                        "type": "attribute_updates",
                        "updates": [
                            {
                                "key": "missing_attribute",
                                "value": "value",
                            }
                        ],
                    },
                    {
                        "type": "add_note",
                        "note": "This must not persist.",
                    },
                ],
            )

        self.assertEqual(
            LeadNote.objects.filter(
                lead=self.lead,
                note="This must not persist.",
            ).count(),
            0,
        )

        self.lead.refresh_from_db()

        self.assertEqual(
            self.lead.attributes,
            initial_attributes,
        )

    # ============================================================
    # MULTIPLE ACTIONS
    # ============================================================

    def test_executes_multiple_actions_atomically(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Budget",
            key="budget",
            field_type="text",
            description="Lead budget.",
            options=[],
            display_order=1,
        )

        result = self.executor.execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "attribute_updates",
                    "updates": [
                        {
                            "key": "budget",
                            "value": "50000",
                        }
                    ],
                },
                {
                    "type": "add_note",
                    "note": "Budget captured.",
                },
                {
                    "type": "pipeline_transition",
                    "stage_shift": {
                        "stage_id": str(self.stage_two.id),
                    },
                },
            ],
        )

        self.lead.refresh_from_db()

        self.assertEqual(
            self.lead.attributes["budget"],
            "50000",
        )
        self.assertEqual(
            self.lead.stage_id,
            self.stage_two.id,
        )
        self.assertEqual(
            LeadNote.objects.filter(
                lead=self.lead,
                note="Budget captured.",
            ).count(),
            1,
        )
        self.assertEqual(len(result), 3)