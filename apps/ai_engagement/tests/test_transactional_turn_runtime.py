from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.ai_engagement.models import Document, OrgInfo
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import (
    record_last_asked_requirement,
    state_for_lead,
)
from apps.ai_engagement.services.runtime_state import STATE_KEY
from apps.ai_engagement.services.transactional_decision_reuse import (
    _PRE_RESOLVED_FILE_KEY,
    _PRE_RESOLVED_FILE_STATUS_KEY,
)
from apps.ai_engagement.services.transactional_turn_runtime import (
    _message_state_resolved,
    _requirements_for_turn,
    _resolve_state_before_response,
)
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests
from apps.crm.models import AttributeDefinition, LeadReminder, Stage


class TransactionalTurnRuntimeTests(TestCase):
    setUp = AIEngagementControlTests.setUp
    _inbound = AIEngagementControlTests._inbound

    def _configure_single_requirement(self):
        self.completion_stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="Qualification Complete",
            display_order=95,
            is_active=True,
            ai_on=True,
        )
        org_info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        org_info.qualification_requirements = (
            "[id: lead_system] Where do you currently manage leads?\n"
            "A. WhatsApp chats\n"
            "B. Excel / Sheets\n"
            "C. CRM\n"
            "D. Multiple places\n"
            "All questions are required"
        )
        org_info.engagement_instructions = (
            "Reply naturally and concisely.\n\n"
            "## Attribute mapped\n"
            "lead_system -> Lead Management Tool\n\n"
            "## Stage shifting\n"
            "When all required qualification questions are answered, move to Qualification Complete.\n"
            "Acknowledgment message: \"Thanks, your qualification details are complete.\""
        )
        org_info.bot_languages = "English"
        org_info.ai_enabled = True
        org_info.save()
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Lead Management Tool",
            key="lead_management_tool",
            description=(
                "Where the lead manages incoming leads. Store Multiple places when "
                "the lead clearly uses more than one system."
            ),
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        requirements = compile_qualification_requirements(
            org_info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        return requirements

    def _document(self, *, active=True):
        return Document.objects.create(
            organization=self.organization,
            name="SHVYA brochure",
            source_key="shvya-brochure",
            version=1,
            file=SimpleUploadedFile(
                "shvya-brochure.pdf",
                b"verified brochure",
                content_type="application/pdf",
            ),
            source_url="",
            share_instruction="Share when the lead explicitly asks for the brochure.",
            processing_status=Document.ProcessingStatus.COMPLETED,
            processing_error="",
            is_active=active,
        )

    def test_final_answer_persists_attribute_then_qualifies_then_moves_stage_and_reminds_once(self):
        requirements = self._configure_single_requirement()
        inbound = self._inbound("txn-final")
        inbound.body = "I use WhatsApp and Excel, and call me tomorrow at 5 PM."
        inbound.save(update_fields=["body"])

        decision = EngagementDecision(
            should_engage=True,
            message="Draft response is not the final response.",
            file_document_id=None,
            crm_actions=[
                {
                    "type": "attribute_updates",
                    "updates": [
                        {"key": "lead_management_tool", "value": "Multiple places"}
                    ],
                },
                {
                    "type": "create_reminder",
                    "title": "Call lead",
                    "description": "Lead requested a call.",
                    "due_at": "2026-09-15T17:00:00+05:30",
                },
            ],
            qualification_updates=[
                {
                    "requirement_id": requirements[0]["id"],
                    "value": "Multiple places",
                    "source_message_id": str(inbound.id),
                    "evidence": "WhatsApp and Excel",
                }
            ],
            next_requirement_id=None,
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            model="test",
        )

        result = _resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.id,
            decision=decision,
        )
        self.assertTrue(result["applied"])
        self.assertTrue(result["final_response_requires_regeneration"])

        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(self.lead.attributes["lead_management_tool"], "Multiple places")
        self.assertEqual(state["qualification_status"], "completed")
        self.assertEqual(self.lead.stage_id, self.completion_stage.id)
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
        self.assertEqual(
            self.lead.attributes[STATE_KEY]["pre_resolved_message_id"],
            str(inbound.id),
        )
        self.assertTrue(
            _message_state_resolved(lead=self.lead, source_message_id=inbound.id)
        )

        duplicate = _resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.id,
            decision=decision,
        )
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "state_already_resolved")
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)

    def test_file_share_is_resolved_and_persisted_before_final_response(self):
        inbound = self._inbound("txn-file")
        inbound.body = "Please send me the brochure."
        inbound.save(update_fields=["body"])
        document = self._document()
        decision = EngagementDecision(
            should_engage=True,
            message="Pre-state draft file reply.",
            file_document_id=document.id,
            crm_actions=[],
            qualification_updates=[],
            next_requirement_id=None,
            reason="ANSWER_ORG_QUESTION",
            reason_code="ANSWER_ORG_QUESTION",
            model="draft-model",
        )

        result = _resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.id,
            decision=decision,
        )

        self.assertTrue(result["applied"])
        self.assertTrue(result["final_response_requires_regeneration"])
        self.assertEqual(result["file_document_id"], document.id)
        self.lead.refresh_from_db()
        runtime = self.lead.attributes[STATE_KEY]
        self.assertEqual(runtime[_PRE_RESOLVED_FILE_KEY], document.id)
        self.assertEqual(
            runtime[_PRE_RESOLVED_FILE_STATUS_KEY],
            "resolved_pending_send",
        )
        self.assertIn("file_share", runtime["pre_resolved_actions"])
        inbound.refresh_from_db()
        processing = inbound.raw_payload["shvya_ai_processing"]
        self.assertTrue(processing["state_resolved"])
        self.assertEqual(processing["resolved_file_document_id"], document.id)
        self.assertEqual(processing["file_share_status"], "resolved_pending_send")

    def test_ineligible_file_fails_before_any_state_is_marked_resolved(self):
        inbound = self._inbound("txn-file-ineligible")
        inbound.body = "Send me that brochure."
        inbound.save(update_fields=["body"])
        document = self._document(active=False)
        decision = EngagementDecision(
            should_engage=True,
            message="This must never be finalized.",
            file_document_id=document.id,
            crm_actions=[],
            qualification_updates=[],
            next_requirement_id=None,
            reason="ANSWER_ORG_QUESTION",
            reason_code="ANSWER_ORG_QUESTION",
            model="draft-model",
        )

        with self.assertRaises(ValueError):
            _resolve_state_before_response(
                organization=self.organization,
                lead=self.lead,
                source_message_id=inbound.id,
                decision=decision,
            )

        self.lead.refresh_from_db()
        runtime = self.lead.attributes.get(STATE_KEY, {})
        self.assertNotEqual(
            str(runtime.get("pre_resolved_message_id") or ""),
            str(inbound.id),
        )
        inbound.refresh_from_db()
        processing = inbound.raw_payload.get("shvya_ai_processing", {})
        self.assertFalse(processing.get("state_resolved"))

    def test_missing_required_answer_does_not_move_to_qualified(self):
        org_info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        org_info.qualification_requirements = (
            "[id: q1] What is your primary goal?\n"
            "[id: q2] What is your expected timeline?\n"
            "All questions are required"
        )
        org_info.engagement_instructions = "Be concise."
        org_info.ai_enabled = True
        org_info.save()
        requirements = compile_qualification_requirements(
            org_info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        inbound = self._inbound("txn-missing")
        inbound.body = "Grow sales"
        inbound.save(update_fields=["body"])

        decision = EngagementDecision(
            should_engage=True,
            message="What is your expected timeline?",
            file_document_id=None,
            crm_actions=[],
            qualification_updates=[
                {
                    "requirement_id": requirements[0]["id"],
                    "value": "Grow sales",
                    "source_message_id": str(inbound.id),
                    "evidence": "Grow sales",
                }
            ],
            next_requirement_id=requirements[1]["id"],
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            model="test",
        )

        result = _resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.id,
            decision=decision,
        )
        self.assertTrue(result["applied"])
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(state["qualification_status"], "in_progress")
        self.assertEqual(self.lead.stage_id, self.new_lead.id)
        self.assertEqual(state["next_requirement_id"], requirements[1]["id"])

    def test_engagement_instruction_qualification_source_is_used_for_persistence(self):
        org_info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        org_info.qualification_requirements = ""
        org_info.engagement_instructions = """
##Qualification criteria
What is your biggest challenge?
A. Slow replies
B. Missed follow-ups
C. Leads going cold
D. No proper tracking
After all required qualification questions are answered, mark the lead qualified.

##Attribute mapped
Q1 -> Challenge
""".strip()
        org_info.ai_enabled = True
        org_info.save()

        requirements = _requirements_for_turn(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(len(requirements), 1)
        self.assertIn("biggest challenge", requirements[0]["question"].casefold())

    def test_completed_qualification_cannot_regress_on_later_message(self):
        requirements = self._configure_single_requirement()
        inbound = self._inbound("txn-complete")
        inbound.body = "B"
        inbound.save(update_fields=["body"])
        decision = EngagementDecision(
            should_engage=True,
            message="Noted.",
            file_document_id=None,
            crm_actions=[
                {
                    "type": "attribute_updates",
                    "updates": [
                        {"key": "lead_management_tool", "value": "Excel / Sheets"}
                    ],
                }
            ],
            qualification_updates=[
                {
                    "requirement_id": requirements[0]["id"],
                    "value": "Excel / Sheets",
                    "source_message_id": str(inbound.id),
                    "evidence": "B",
                }
            ],
            next_requirement_id=None,
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            model="test",
        )
        _resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.id,
            decision=decision,
        )
        self.lead.refresh_from_db()
        before = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(before["qualification_status"], "completed")

        later = self._inbound("txn-later")
        later.body = "What happens next?"
        later.save(update_fields=["body"])
        ordinary = EngagementDecision(
            should_engage=True,
            message="Our team will continue from here.",
            file_document_id=None,
            crm_actions=[],
            qualification_updates=[],
            next_requirement_id=None,
            reason="NORMAL_CONVERSATION",
            reason_code="NORMAL_CONVERSATION",
            model="test",
        )
        _resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=later.id,
            decision=ordinary,
        )
        self.lead.refresh_from_db()
        after = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(after["qualification_status"], "completed")
        self.assertEqual(self.lead.stage_id, self.completion_stage.id)
