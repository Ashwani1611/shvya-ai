from __future__ import annotations

from types import SimpleNamespace

from django.test import TestCase

from apps.ai_engagement.services import conversation_policy_runtime, trace_service
from apps.ai_engagement.services.action_planner import ActionPlanner, STATUS_ACCEPTED
from apps.ai_engagement.services.evidence_resolver import (
    EvidenceItem,
    EvidenceResolution,
    GroundingCategory,
    InformationClass,
)
from apps.ai_engagement.services.intent_engine import (
    ClassificationPath,
    Intent,
    IntentDecision,
    IntentEngine,
)
from apps.ai_engagement.services.phase7_completion_runtime import (
    _deterministically_supported_reply,
)
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests


MASTER_MESSAGE = (
    "We receive around 30 leads daily. What is your pricing? Please call me tomorrow."
)


class _NoModelProvider:
    def __init__(self):
        self.calls = []

    def generate_text(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("Obvious deterministic intent must not call the model.")


class Phase7MasterIntentTests(TestCase):
    def test_master_scenario_is_transport_neutral_and_zero_intent_model_calls(self):
        organization = SimpleNamespace(id="org-a")
        lead = SimpleNamespace(
            id="lead-a",
            organization_id="org-a",
            attributes={},
        )
        requirements = [
            {
                "id": "volume",
                "label": "Daily lead volume",
                "question": "How many leads do you receive daily?",
            },
            {
                "id": "ads",
                "label": "Running ads",
                "question": "Are you running ads?",
            },
        ]
        state = {
            "current_requirement_id": "volume",
            "last_asked_requirement_id": "volume",
        }
        provider = _NoModelProvider()
        outputs = []

        for transport in ("api", "hosted", "coexistence"):
            context = SimpleNamespace(
                organization={"id": "org-a"},
                lead={"id": "lead-a"},
                transport=transport,
            )
            decision = IntentEngine(provider=provider).classify(
                organization=organization,
                lead=lead,
                message=MASTER_MESSAGE,
                source_message_id="m-master",
                requirements=requirements,
                qualification_state=state,
                context=context,
            )
            outputs.append(decision.as_dict())

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        result = outputs[0]
        self.assertEqual(result["primary_intent"], Intent.CALL_REQUEST.value)
        self.assertIn(Intent.PRICING_QUESTION.value, result["secondary_intents"])
        self.assertIn(Intent.QUALIFICATION_ANSWER.value, result["secondary_intents"])
        self.assertEqual(result["qualification_candidate"]["value"], 30)
        self.assertTrue(result["requires_knowledge"])
        self.assertTrue(result["requires_human"])
        self.assertEqual(result["classification_path"], ClassificationPath.DETERMINISTIC.value)
        self.assertEqual(provider.calls, [])

    def test_call_budget_for_obvious_intent_families_is_zero(self):
        organization = SimpleNamespace(id="org-a")
        lead = SimpleNamespace(id="lead-a", organization_id="org-a", attributes={})
        requirements = [
            {"id": "volume", "question": "How many leads do you receive daily?"},
        ]
        cases = [
            ("Hi", {}),
            (
                "30",
                {
                    "current_requirement_id": "volume",
                    "last_asked_requirement_id": "volume",
                },
            ),
            ("What is your pricing?", {}),
            ("Please call me tomorrow.", {}),
        ]
        provider = _NoModelProvider()
        for text, state in cases:
            decision = IntentEngine(provider=provider).classify(
                organization=organization,
                lead=lead,
                message=text,
                source_message_id="m-budget",
                requirements=requirements,
                qualification_state=state,
            )
            self.assertEqual(decision.classification_path, ClassificationPath.DETERMINISTIC)
        self.assertEqual(provider.calls, [])


class Phase7PlannerProvenanceTests(TestCase):
    setUp = AIEngagementControlTests.setUp
    _inbound = AIEngagementControlTests._inbound

    def test_executor_planner_boundary_recovers_phase2_to_6_provenance(self):
        inbound = self._inbound("phase7-master-provenance")
        inbound.body = MASTER_MESSAGE
        inbound.save(update_fields=["body"])

        intent = IntentDecision(
            primary_intent=Intent.PRICING_QUESTION,
            secondary_intents=(Intent.QUALIFICATION_ANSWER, Intent.CALL_REQUEST),
            confidence=0.99,
            facts=(
                {
                    "key": "lead_volume",
                    "requirement_id": "volume",
                    "value": 30,
                    "confidence": 0.99,
                    "source_message_id": str(inbound.id),
                    "source_type": "phase2_intent",
                },
            ),
            qualification_candidate={
                "key": "lead_volume",
                "requirement_id": "volume",
                "value": 30,
                "confidence": 0.99,
                "evidence": "around 30 leads daily",
            },
            direct_question="What is your pricing?",
            requested_action="call",
            classification_path=ClassificationPath.DETERMINISTIC,
            requires_knowledge=True,
            requires_human=True,
            model="deterministic",
        )
        turn_token = conversation_policy_runtime._TURN.set(
            {
                "organization_id": str(self.organization.id),
                "lead_id": str(self.lead.id),
                "source_message_id": str(inbound.id),
                "intent_decision": intent,
            }
        )
        policy_token = conversation_policy_runtime._POLICY.set(
            SimpleNamespace(
                outcome=SimpleNamespace(value="ANSWER_THEN_QUALIFY"),
                reason_code="DIRECT_QUESTION_THEN_QUALIFY",
                policy_source="deterministic_python",
            )
        )
        trace_token = trace_service.begin_trace(
            organization=self.organization,
            lead=self.lead,
            source_message=inbound,
        )
        trace_service.record(
            "grounding",
            {
                "category": "STRUCTURED_ORG_DATA",
                "question_type": "pricing",
                "verified": True,
                "sources": [
                    {
                        "source_id": "org:pricing",
                        "source_type": "organization_profile",
                        "score": 1.0,
                        "metadata": {"key": "pricing"},
                    }
                ],
            },
        )

        try:
            plan = ActionPlanner().plan(
                organization=self.organization,
                lead=self.lead,
                decision=SimpleNamespace(
                    crm_actions=[
                        {
                            "type": "add_note",
                            "note": "Lead requested pricing and a callback.",
                        }
                    ],
                    file_document_id=None,
                    reason_code="NORMAL_CONVERSATION",
                ),
            )
        finally:
            trace_service._CURRENT.reset(trace_token)
            conversation_policy_runtime._POLICY.reset(policy_token)
            conversation_policy_runtime._TURN.reset(turn_token)

        self.assertEqual(plan.source_message_id, str(inbound.id))
        self.assertIn("PRICING_QUESTION", plan.source_intent)
        self.assertIn("QUALIFICATION_ANSWER", plan.source_intent)
        self.assertIn("CALL_REQUEST", plan.source_intent)
        self.assertEqual(plan.source_policy, "DIRECT_QUESTION_THEN_QUALIFY")
        self.assertEqual(plan.policy_outcome, "ANSWER_THEN_QUALIFY")
        self.assertEqual(plan.evidence_references[1]["source_id"], "org:pricing")
        self.assertEqual(plan.actions[0].validation_status, STATUS_ACCEPTED)
        self.assertEqual(plan.actions[0].source_evidence[0]["message_id"], str(inbound.id))


class DeterministicGroundingBudgetTests(TestCase):
    def _resolution(self):
        return EvidenceResolution(
            category=GroundingCategory.STRUCTURED_ORG_DATA,
            information_class=InformationClass.STATIC_CONFIGURED,
            question_type="pricing",
            sensitive=True,
            verified=True,
            evidence=(
                EvidenceItem(
                    source_id="org:pricing",
                    source_type="organization_profile",
                    content="Pro plan price 999 per month",
                    score=1.0,
                ),
            ),
        )

    def test_exact_verified_pricing_answer_skips_secondary_verifier(self):
        decision = SimpleNamespace(
            should_engage=True,
            message="Pro plan price 999 per month",
            crm_actions=[],
            qualification_updates=[],
            file_document_id=None,
        )
        self.assertTrue(
            _deterministically_supported_reply(decision, self._resolution())
        )

    def test_unsupported_pricing_claim_does_not_bypass_verifier(self):
        decision = SimpleNamespace(
            should_engage=True,
            message="Pro plan price 999 per month guaranteed discount",
            crm_actions=[],
            qualification_updates=[],
            file_document_id=None,
        )
        self.assertFalse(
            _deterministically_supported_reply(decision, self._resolution())
        )
