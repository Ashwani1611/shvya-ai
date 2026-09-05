from __future__ import annotations

from unittest.mock import Mock

from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.engagement import (
    EngagementDecision,
    EngagementError,
    EngagementService,
)
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class EngagementServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Engagement Test Organization",
        )

        cls.org_info = OrgInfo.objects.create(
            organization=cls.organization,
            about=(
                "Cybersecurity training academy "
                "providing certification programs."
            ),
            bot_languages="English, Hindi, Hinglish",
            qualification_requirements=(
                "Identify course interest, budget, "
                "timeline, and buying intent."
            ),
            engagement_instructions=(
                "Use a friendly, concise, and helpful tone."
            ),
            ai_enabled=True,
            bump_up_enabled=True,
            bump_up_count=2,
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Engagement Test Pipeline",
            description="Pipeline used for engagement tests.",
            is_active=True,
        )

        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New Lead",
            description="New incoming lead.",
            display_order=0,
            is_active=True,
        )

        cls.lead = Lead.objects.create(
            organization=cls.organization,
            pipeline=cls.pipeline,
            stage=cls.stage,
            name="Engagement Test Lead",
            phone="+919876543210",
            email="lead@example.com",
            notes="Existing lead note.",
            attributes={
                "course_interest": "Cybersecurity",
            },
            lead_source="whatsapp_api",
        )

    def build_context(self):
        return AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
        )

    def mock_provider(
        self,
        payload: str,
    ):
        provider = Mock()

        provider.generate_text.return_value = (
            AITextResult(
                text=payload,
                model="gpt-4.1-nano",
            )
        )

        return provider

    def test_engage_returns_normalized_decision(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Thanks for reaching out!",
                "file_document_id": null,
                "crm_actions": [],
                "reason": "A customer-facing response is appropriate."
            }
            """
        )
        service = EngagementService(provider=provider)
        decision = service.engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertIsInstance(decision, EngagementDecision)
        self.assertTrue(decision.should_engage)
        self.assertEqual(decision.message, "Thanks for reaching out!")
        self.assertIsNone(decision.file_document_id)
        self.assertEqual(decision.crm_actions, [])
        self.assertEqual(
            decision.reason,
            "A customer-facing response is appropriate.",
        )
        self.assertEqual(decision.model, "gpt-4.1-nano")
        provider.generate_text.assert_called_once()

    def test_no_engagement_requires_empty_message(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": false,
                "message": "",
                "file_document_id": null,
                "crm_actions": [],
                "reason": "No customer-facing response is needed."
            }
            """
        )
        service = EngagementService(provider=provider)
        decision = service.engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertFalse(decision.should_engage)
        self.assertEqual(decision.message, "")

    def test_provider_receives_context_and_instructions(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Hello!",
                "file_document_id": null,
                "crm_actions": [],
                "reason": "Response needed."
            }
            """
        )
        service = EngagementService(provider=provider)
        service.engage(
            organization=self.organization,
            lead=self.lead,
        )
        call = provider.generate_text.call_args
        instructions = call.kwargs["instructions"]
        input_text = call.kwargs["input_text"]
        self.assertIn("SHVYA", instructions)
        self.assertIn("customer-facing", instructions.lower())
        self.assertIn(self.org_info.engagement_instructions, instructions)
        self.assertLess(
            instructions.index(self.org_info.engagement_instructions),
            instructions.index("SHVYA AI ENGAGEMENT TASK"),
        )
        self.assertIn(self.organization.name, input_text)
        self.assertIn(self.lead.name, input_text)
        self.assertIn(self.org_info.engagement_instructions, instructions)
        self.assertIn(self.org_info.qualification_requirements, input_text)

    def test_accepts_attribute_update_request(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Thanks!",
                "file_document_id": null,
                "crm_actions": [
                    {
                        "type": "attribute_updates",
                        "updates": [
                            {"key": "budget", "value": "50000"}
                        ]
                    }
                ],
                "reason": "Lead supplied a budget."
            }
            """
        )
        decision = EngagementService(provider=provider).engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(decision.crm_actions[0]["type"], "attribute_updates")

    def test_accepts_stage_shift_request(self):
        target_stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="Qualified",
            description="Qualified lead.",
            display_order=100,
            is_active=True,
        )
        provider = self.mock_provider(
            f"""
            {{
                "should_engage": true,
                "message": "Great, thank you.",
                "file_document_id": null,
                "crm_actions": [
                    {{
                        "type": "pipeline_transition",
                        "stage_shift": {{"stage_id": "{target_stage.id}"}}
                    }}
                ],
                "reason": "The conversation supports a stage transition."
            }}
            """
        )
        decision = EngagementService(provider=provider).engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(decision.crm_actions[0]["type"], "pipeline_transition")
        self.assertEqual(
            decision.crm_actions[0]["stage_shift"]["stage_id"],
            str(target_stage.id),
        )

    def test_accepts_add_note_request(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Thank you!",
                "file_document_id": null,
                "crm_actions": [{"type": "add_note", "note": "Lead confirmed interest."}],
                "reason": "Conversation contains useful CRM information."
            }
            """
        )
        decision = EngagementService(provider=provider).engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(decision.crm_actions[0]["type"], "add_note")

    def test_accepts_create_reminder_request(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "I will follow up with you.",
                "file_document_id": null,
                "crm_actions": [
                    {
                        "type": "create_reminder",
                        "title": "Follow up with lead",
                        "description": "Discuss course enrollment.",
                        "due_at": "2026-09-05T10:00:00+05:30"
                    }
                ],
                "reason": "A follow-up is explicitly supported."
            }
            """
        )
        decision = EngagementService(provider=provider).engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(decision.crm_actions[0]["type"], "create_reminder")

    def test_accepts_contact_update_request(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Thanks for the updated contact details.",
                "file_document_id": null,
                "crm_actions": [
                    {
                        "type": "contact_updates",
                        "updates": [
                            {
                                "contact_id": "existing-contact-id",
                                "channel": "whatsapp",
                                "handle": "+919999999999"
                            }
                        ]
                    }
                ],
                "reason": "Lead supplied updated contact information."
            }
            """
        )
        decision = EngagementService(provider=provider).engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(decision.crm_actions[0]["type"], "contact_updates")

    def test_accepts_file_document_id(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "I can share the course brochure.",
                "file_document_id": 42,
                "crm_actions": [],
                "reason": "The document is relevant."
            }
            """
        )
        decision = EngagementService(provider=provider).engage(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(decision.file_document_id, 42)

    def test_rejects_extra_top_level_field(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Hello!",
                "file_document_id": null,
                "crm_actions": [],
                "reason": "Response needed.",
                "internal_reasoning": "secret"
            }
            """
        )
        service = EngagementService(provider=provider)
        with self.assertRaises(EngagementError):
            service.engage(
                organization=self.organization,
                lead=self.lead,
            )

    def test_rejects_invalid_json(self):
        service = EngagementService(
            provider=self.mock_provider("not valid json")
        )
        with self.assertRaises(EngagementError):
            service.engage(
                organization=self.organization,
                lead=self.lead,
            )

    def test_rejects_empty_message_when_engaging(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "",
                "file_document_id": null,
                "crm_actions": [],
                "reason": "Response needed."
            }
            """
        )
        with self.assertRaises(EngagementError):
            EngagementService(provider=provider).engage(
                organization=self.organization,
                lead=self.lead,
            )

    def test_rejects_message_when_not_engaging(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": false,
                "message": "Hello!",
                "file_document_id": null,
                "crm_actions": [],
                "reason": "No response needed."
            }
            """
        )
        with self.assertRaises(EngagementError):
            EngagementService(provider=provider).engage(
                organization=self.organization,
                lead=self.lead,
            )

    def test_rejects_unknown_crm_action(self):
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Hello!",
                "file_document_id": null,
                "crm_actions": [{"type": "delete_lead"}],
                "reason": "Unsupported action."
            }
            """
        )
        with self.assertRaises(EngagementError):
            EngagementService(provider=provider).engage(
                organization=self.organization,
                lead=self.lead,
            )

    def test_rejects_context_for_different_lead(self):
        other_lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Other Lead",
            phone="+919876543211",
            lead_source="whatsapp_api",
        )
        context = self.build_context()
        provider = self.mock_provider(
            """
            {
                "should_engage": true,
                "message": "Hello!",
                "file_document_id": null,
                "crm_actions": [],
                "reason": "Response needed."
            }
            """
        )
        with self.assertRaises(EngagementError):
            EngagementService(provider=provider).engage(
                organization=self.organization,
                lead=other_lead,
                context=context,
            )
