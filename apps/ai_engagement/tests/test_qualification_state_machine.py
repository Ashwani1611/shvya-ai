from __future__ import annotations

from django.test import TestCase

from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_state import (
    QUALIFICATION_STATE_KEY,
    REQUIREMENT_ANSWERED,
    REQUIREMENT_ASKED,
    STATUS_COMPLETED,
    apply_unambiguous_reply,
    project_answer_updates,
    record_last_asked_requirement,
    requirements_for_lead,
    state_for_lead,
)
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class QualificationStateMachineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="State Machine Org")
        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Sales",
            is_active=True,
        )
        cls.new_stage = (
            cls.pipeline.stages.filter(name__in=["New Lead", "New leads"])
            .order_by("display_order", "name")
            .first()
        )
        if cls.new_stage is None:
            used = set(cls.pipeline.stages.values_list("display_order", flat=True))
            order = 0
            while order in used:
                order += 1
            cls.new_stage = Stage.objects.create(
                pipeline=cls.pipeline,
                name="New Lead",
                display_order=order,
                is_active=True,
            )
        if not cls.pipeline.stages.filter(name__iexact="Qualified").exists():
            used = set(cls.pipeline.stages.values_list("display_order", flat=True))
            order = 0
            while order in used:
                order += 1
            Stage.objects.create(
                pipeline=cls.pipeline,
                name="Qualified",
                display_order=order,
                is_active=True,
            )

    def setUp(self):
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Test Lead",
            phone="+919900000123",
            attributes={},
            ai_enabled=True,
        )

    def _requirements(self):
        return compile_qualification_requirements(
            "What is your biggest challenge?\n"
            "Where do you manage leads?\n"
            "A. WhatsApp\n"
            "B. Excel\n"
            "C. CRM\n"
            "D. Multiple places\n"
            "How many leads do you typically receive per day?\n"
            "A. 0-10\n"
            "B. 10-30\n"
            "C. 30+\n"
            "Do you currently run Meta ads?\n"
            "A. Yes\n"
            "B. No"
        )["requirements"]

    def test_requirement_has_stable_identity_separate_from_wording(self):
        first = compile_qualification_requirements(
            "How many leads do you typically receive per day?\nDo you run ads?"
        )
        edited = compile_qualification_requirements(
            "How many leads do you usually receive each day?\nDo you currently run ads?"
        )

        self.assertNotEqual(
            first["requirements"][0]["id"],
            edited["requirements"][0]["id"],
        )
        self.assertEqual(
            first["requirements"][0]["stable_id"],
            edited["requirements"][0]["stable_id"],
        )
        self.assertEqual(first["requirements"][0]["stable_id"], "qualification_1")
        self.assertNotEqual(first["flow_version"], edited["flow_version"])

    def test_option_aliases_apply_only_to_persisted_active_requirement(self):
        requirements = self._requirements()
        q1, q2 = requirements[0], requirements[1]
        record_last_asked_requirement(self.lead, q1["id"], requirements=requirements)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="Slow replies",
            source_message_id="msg-q1",
        )
        record_last_asked_requirement(self.lead, q2["id"], requirements=requirements)

        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="b",
            source_message_id="msg-q2",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["state"]["requirement_states"][q2["id"]]["value"],
            "Excel",
        )
        self.assertEqual(
            result["state"]["current_requirement_id"],
            requirements[2]["id"],
        )

    def test_duplicate_message_id_cannot_advance_two_requirements(self):
        requirements = self._requirements()
        first = requirements[0]
        record_last_asked_requirement(self.lead, first["id"], requirements=requirements)

        first_result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="Slow replies",
            source_message_id="same-message",
        )
        self.assertTrue(first_result["changed"])
        second_id = first_result["state"]["current_requirement_id"]
        record_last_asked_requirement(self.lead, second_id, requirements=requirements)

        duplicate = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="Slow replies",
            source_message_id="same-message",
        )
        self.assertFalse(duplicate["changed"])
        self.assertTrue(duplicate.get("duplicate"))
        self.assertEqual(duplicate["state"]["current_requirement_id"], second_id)
        self.assertNotEqual(
            duplicate["state"]["requirement_states"][second_id]["status"],
            REQUIREMENT_ANSWERED,
        )

    def test_information_question_does_not_answer_or_reset_active_requirement(self):
        requirements = self._requirements()
        first = requirements[0]
        record_last_asked_requirement(self.lead, first["id"], requirements=requirements)

        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="What is SHVYA?",
            source_message_id="info-message",
        )
        self.assertFalse(result["changed"])
        self.assertEqual(result["state"]["current_requirement_id"], first["id"])
        self.assertEqual(
            result["state"]["requirement_states"][first["id"]]["status"],
            REQUIREMENT_ASKED,
        )

    def test_call_request_does_not_answer_or_reset_unrelated_requirement(self):
        requirements = self._requirements()
        first = requirements[0]
        record_last_asked_requirement(self.lead, first["id"], requirements=requirements)

        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="I booked a call, please have someone speak with me",
            source_message_id="call-message",
        )
        self.assertFalse(result["changed"])
        self.assertEqual(result["state"]["current_requirement_id"], first["id"])

    def test_yes_only_answers_active_boolean_requirement(self):
        requirements = self._requirements()
        first = requirements[0]
        record_last_asked_requirement(self.lead, first["id"], requirements=requirements)
        non_boolean = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="yes",
            source_message_id="not-boolean",
        )
        self.assertFalse(non_boolean["changed"])
        self.assertEqual(non_boolean["state"]["current_requirement_id"], first["id"])

    def test_in_progress_flow_is_pinned_across_ai_setup_wording_edits(self):
        original = compile_qualification_requirements(
            "Which city are you in?\nWhat is your budget?"
        )["requirements"]
        record_last_asked_requirement(self.lead, original[0]["id"], requirements=original)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=original,
            text="Gurugram",
            source_message_id="city-message",
        )

        edited = compile_qualification_requirements(
            "Which city are you located in currently?\nWhat budget range do you have?"
        )["requirements"]
        active = requirements_for_lead(self.lead, edited)
        self.assertEqual(active[0]["question"], original[0]["question"])
        self.assertEqual(active[1]["question"], original[1]["question"])

        state = state_for_lead(self.lead, requirements=edited)
        self.assertEqual(state["current_requirement_id"], original[1]["id"])
        self.assertIn(original[0]["id"], state["answered_requirement_ids"])

    def test_out_of_order_evidence_can_be_stored_without_skipping_current(self):
        requirements = compile_qualification_requirements(
            "Which city are you in?\nWhere do you manage leads?\nHow many leads per day?"
        )["requirements"]
        q1, q2, q3 = requirements
        record_last_asked_requirement(self.lead, q1["id"], requirements=requirements)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="Gurugram",
            source_message_id="q1",
        )
        record_last_asked_requirement(self.lead, q2["id"], requirements=requirements)
        state = state_for_lead(self.lead, requirements=requirements)

        projected = project_answer_updates(
            state=state,
            requirements=requirements,
            updates=[{
                "requirement_id": q3["id"],
                "value": "30 leads",
                "source_message_id": "out-of-order",
                "evidence": "30 leads",
            }],
            messages=[{
                "id": "out-of-order",
                "direction": "inbound",
                "body": "We get 30 leads daily",
            }],
        )
        self.assertEqual(
            projected["requirement_states"][q3["id"]]["status"],
            REQUIREMENT_ANSWERED,
        )
        self.assertEqual(projected["current_requirement_id"], q2["id"])

    def test_completion_clears_pending_question_and_never_reopens(self):
        requirements = compile_qualification_requirements(
            "Do you run ads?\nA. Yes\nB. No"
        )["requirements"]
        only = requirements[0]
        record_last_asked_requirement(self.lead, only["id"], requirements=requirements)
        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="A",
            source_message_id="final-answer",
        )
        self.assertEqual(result["state"]["qualification_status"], STATUS_COMPLETED)
        self.assertIsNone(result["state"]["current_requirement_id"])
        self.assertIsNone(result["state"]["next_requirement_id"])

        record_last_asked_requirement(self.lead, only["id"], requirements=requirements)
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(state["qualification_status"], STATUS_COMPLETED)
        self.assertIsNone(state["current_requirement_id"])
        self.assertEqual(
            state["requirement_states"][only["id"]]["status"],
            REQUIREMENT_ANSWERED,
        )
        self.assertTrue(self.lead.attributes[QUALIFICATION_STATE_KEY]["history"])
