"""A fresh authored flow permits capture before questions, including handoff turns."""
import json
from copy import deepcopy
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import (
    EngagementDecision, EngagementError, EngagementService,
)
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
from apps.ai_engagement.services.conversation_policy import ConversationPolicyDecision, ConversationPolicyOutcome
from apps.ai_engagement.services.conversation_policy_runtime import _POLICY
from apps.ai_engagement.services.qualification_state import project_answer_updates, state_for_lead
from apps.ai_engagement.tests import test_precise_orchestration as context_fixtures
from tests.playbook_fixtures import build_ai_playbook


# Necessary authored question content from the deployed Brain readback. Compile
# its numbered questions and option meanings rather than supplying artificial
# requirement IDs or a pre-started flow snapshot.
_QUESTIONS = """1. What’s your biggest issue with managing or converting leads right now?
A) Slow replies
B) Missed follow-ups
C) Leads going cold
D) No proper tracking

2. Where do you currently manage your leads?
A) WhatsApp
B) Excel / Google Sheets
C) CRM
D) Multiple places

3. Approximately how many leads do you receive per day?
A) 0–10
B) 11–30
C) More than 30

4. Are you currently running paid ads?
A) Yes
B) No
"""


class FirstTurnVolunteeredCaptureTests(SimpleTestCase):
    def _turn(self, *, channel="whatsapp", callback=False, body=None):
        problem = "missed follow-ups" if channel == "instagram" else "slow replies"
        tool = "Google Sheets" if channel == "instagram" else "WhatsApp"
        body = body or (
            f"My biggest problem is {problem}. I manage leads in {tool}, receive 20 leads per day, "
            "and I am not currently running paid ads."
        )
        if callback:
            body += " Please call me tomorrow at 3 PM India time and share the product brochure."
        context = context_fixtures.PreciseEngagementTests()._context(body)
        context.organization["ai_playbook"] = build_ai_playbook(
            questions=_QUESTIONS, rules="Save supported answers as they arrive. Ask one question at a time.",
        )
        context.stage.update(id="new-stage", name="New leads")
        context.conversation.update(channel=channel, execution_mode="sandbox_preview")
        context.conversation["messages"][0]["id"] = "fresh-inbound"
        context.lead.update(phone="", lead_source=channel)
        profile = compile_org_ai_profile_from_context(context.organization)
        requirements = profile["qualification"]["requirements"]
        self.assertEqual(len(requirements), 4)
        state = state_for_lead(
            SimpleNamespace(attributes={}, stage=SimpleNamespace(name="New leads")), requirements=requirements,
        )
        self.assertEqual(state["flow_snapshot"], [])
        payload = json.loads(EngagementService()._build_input(
            context=context, profile=profile, qualification_state=state, next_item=requirements[0],
        ))
        return context, requirements, state, payload

    def _updates(self, context, requirements):
        instagram = context.conversation["channel"] == "instagram"
        values = ("Missed follow-ups" if instagram else "Slow replies",
                  "Excel / Google Sheets" if instagram else "WhatsApp", "11–30", "No")
        evidence = ("missed follow-ups" if instagram else "slow replies",
                    "Google Sheets" if instagram else "WhatsApp", "20 leads per day",
                    "I am not currently running paid ads")
        return [
            {"requirement_id": requirement["id"], "value": value,
             "source_message_id": "fresh-inbound", "evidence": quote}
            for requirement, value, quote in zip(requirements, values, evidence, strict=True)
        ]

    def _decision(self, updates):
        return EngagementDecision(
            should_engage=True, message="Thanks for sharing your setup.", file_document_id=None,
            crm_actions=[], qualification_updates=updates, next_requirement_id=None,
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="recorded-draft",
        )

    def _validate(self, *, context, requirements, state, updates):
        callback = "Please call me" in context.conversation["messages"][0]["body"]
        token = _POLICY.set(ConversationPolicyDecision(
            outcome=ConversationPolicyOutcome.CALL_HANDOFF,
            reason_code="CALL_REQUEST_SUPPORTED", confidence=1.0, requires_human=True,
            handoff_type="call", allowed_response_goal="call_handoff",
        ) if callback else None)
        try:
            EngagementService()._validate_qualification_decision(
                decision=self._decision(updates), context=context,
                requirements=requirements, qualification_state=state,
            )
        finally:
            _POLICY.reset(token)

    def test_fresh_flow_exposes_future_capture_goals_on_ordinary_and_callback_turns(self):
        for channel in ("whatsapp", "instagram"):
            for callback in (False, True):
                with self.subTest(channel=channel, callback=callback):
                    _context, requirements, _state, payload = self._turn(channel=channel, callback=callback)
                    turn = payload["qualification_turn"]
                    self.assertEqual([item["id"] for item in turn["unanswered_requirements_for_evidence"]],
                                     [item["id"] for item in requirements])
                    self.assertEqual([item["id"] for item in turn["capture_only_requirements"]],
                                     [item["id"] for item in requirements[2:]])
                    self.assertTrue(all(not item["askable"] for item in turn["capture_only_requirements"]))
                    self.assertEqual(turn["capture_only_requirements"][0]["options"], requirements[2]["options"])

    def test_all_four_source_backed_answers_complete_without_another_question(self):
        for channel in ("whatsapp", "instagram"):
            for callback in (False, True):
                with self.subTest(channel=channel, callback=callback):
                    context, requirements, state, _payload = self._turn(channel=channel, callback=callback)
                    updates = self._updates(context, requirements)
                    self._validate(context=context, requirements=requirements, state=state, updates=updates)
                    projected = project_answer_updates(
                        state=state, requirements=requirements, updates=updates,
                        messages=context.conversation["messages"],
                    )
                    self.assertTrue(projected["qualification_completed"])
                    self.assertEqual(projected["qualification_answers"],
                                     {item["requirement_id"]: item["value"] for item in updates})
                    self.assertIsNone(projected["next_requirement_id"])
                    self.assertEqual(context.lead["phone"], "")

    def test_partial_volunteered_capture_is_allowed_during_handoff(self):
        context, requirements, state, _payload = self._turn(callback=True)
        updates = self._updates(context, requirements)[:1]
        self._validate(context=context, requirements=requirements, state=state, updates=updates)
        projected = project_answer_updates(state=state, requirements=requirements, updates=updates,
                                          messages=context.conversation["messages"])
        self.assertFalse(projected["qualification_completed"])
        self.assertEqual(projected["answered_requirement_ids"], [requirements[0]["id"]])

    def test_capture_still_requires_current_inbound_source_and_exact_evidence(self):
        context, requirements, state, _payload = self._turn(callback=True)
        for field, value in (("source_message_id", "different-inbound"), ("evidence", "running ads on Google")):
            with self.subTest(field=field):
                updates = deepcopy(self._updates(context, requirements))
                updates[0][field] = value
                with self.assertRaisesRegex(EngagementError, "inbound evidence"):
                    self._validate(context=context, requirements=requirements, state=state, updates=updates)
