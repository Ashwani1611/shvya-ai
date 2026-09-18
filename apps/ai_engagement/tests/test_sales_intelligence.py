import json
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import LeadSignal, InternalConversationSummary
from apps.ai_engagement.services.customer_memory import explicit_facts
from apps.ai_engagement.services.lead_intelligence import observe_accepted_turn, signal_summary
from apps.ai_engagement.services.sales_intelligence import ObjectionEngine, detect_signals, score_signals
from apps.ai_engagement.services.intent_types import Intent, IntentDecision
from apps.ai_engagement.services.structured_memory import StructuredLeadMemoryService, MEMORY_KEY
from apps.ai_engagement.services.tenant_guard import TenantScopeError
from apps.ai_engagement.tests import test_engagement_controls as fixtures
from apps.crm.models import Lead, Pipeline, AttributeDefinition
from apps.organizations.models import Organization


class ObjectionSignalPolicyTests(SimpleTestCase):
    def test_canonical_objections_are_deterministic_without_canned_business_answers(self):
        cases = {"Too expensive": "PRICE_TOO_HIGH", "I need time": "NEED_TIME",
                 "Comparing options": "COMPARING_OPTIONS", "I need approval": "NEED_APPROVAL",
                 "I have no budget": "NO_BUDGET", "Maybe later": "NOT_NOW",
                 "Can I trust this?": "TRUST_CONCERN", "Missing a feature": "FEATURE_MISSING",
                 "I already use a system": "ALREADY_USING_ALTERNATIVE", "Not interested": "NOT_INTERESTED"}
        for text, expected in cases.items():
            with self.subTest(category=expected):
                output = ObjectionEngine().detect(text=text)
                self.assertIn(expected, [item.category for item in output])
                self.assertTrue(all(not item.approved_facts for item in output))
                self.assertTrue(all(not item.strategy for item in output))
        other = ObjectionEngine().detect(text="An unusual concern", intent_decision=IntentDecision(primary_intent=Intent.OBJECTION))
        self.assertEqual(other[0].category, "OTHER")
        self.assertEqual(ObjectionEngine().detect(text="Not too expensive"), ())

    def test_organization_rules_do_not_cross_or_invent_offers(self):
        settings = {"ai_objections": {"categories": {"PRICE_TOO_HIGH": {
            "strategy": "Explain the included onboarding", "approved_facts": ["Onboarding is included."],
            "discount": "A 5% annual-payment discount is approved.", "escalate": True}}}}
        first = ObjectionEngine().detect(text="Too expensive", settings=settings)[0]
        second = ObjectionEngine().detect(text="Too expensive", settings={})[0]
        self.assertTrue(first.escalate)
        self.assertEqual(len(first.approved_facts), 2)
        self.assertEqual(second.approved_facts, ())
        self.assertNotIn("10%", str(first.as_dict()))

    def test_language_and_literal_custom_rules(self):
        self.assertEqual(ObjectionEngine().detect(text="bahut mehenga")[0].category, "PRICE_TOO_HIGH")
        self.assertEqual(ObjectionEngine().detect(text="बजट नहीं है")[0].category, "NO_BUDGET")
        cfg = {"ai_objections": {"categories": {"NEED_APPROVAL": {"phrases": ["comité validation"]}}}}
        self.assertEqual(ObjectionEngine().detect(text="comité validation", settings=cfg)[0].category, "NEED_APPROVAL")
        self.assertEqual(ObjectionEngine().detect(text="Too expensive", settings={"ai_objections": {"enabled": False}}), ())

    def test_only_explicit_customer_budget_not_company_price_is_extracted(self):
        self.assertEqual(explicit_facts(text="Your price is 999", source_message_id="a"), [])
        result = explicit_facts(text="My budget is ₹50000. We receive around 25 leads daily. We use Excel.", source_message_id="b")
        facts = {item["concept"]: item for item in result}
        self.assertEqual(facts["budget"]["value"], "₹50000")
        self.assertEqual(facts["lead_volume"]["value"], 25)
        self.assertEqual(facts["current_tools"]["value"], "Excel")

    def test_signals_are_explainable_bounded_and_never_qualification(self):
        signals = detect_signals(text="Please schedule a demo. What is your pricing?", repeated=True)
        self.assertIn(("requested_demo", ""), signals)
        self.assertIn(("asked_pricing", ""), signals)
        result = score_signals(counts={"requested_demo": 100, "asked_pricing": 20}, settings={})
        self.assertEqual(result["score"], 40)
        self.assertTrue(all(item["credited_count"] == 1 for item in result["components"]))
        self.assertFalse(result["qualification_override"])
        tuned = score_signals(counts={"requested_demo": 1}, settings={"ai_signals": {"weights": {"requested_demo": 7}}})
        self.assertEqual(tuned["score"], 7)
        self.assertEqual(score_signals(counts={"opt_out": 1, "asked_pricing": 1})["score"], 0)
        invalid = score_signals(counts={"asked_pricing": 1}, settings={"ai_signals": {"weights": {"asked_pricing": float("nan")}}})
        self.assertEqual(invalid["score"], 15)


class AcceptedLeadIntelligenceTests(TestCase):
    setUp = fixtures.AIEngagementControlTests.setUp
    _inbound = fixtures.AIEngagementControlTests._inbound

    def source(self, body, key="intelligence"):
        source = self._inbound(key)
        source.body = body
        source.save(update_fields=["body"])
        return source

    def test_same_source_memory_events_and_signals_are_idempotent_without_model_calls(self):
        source = self.source("My budget is ₹50000. I have already booked tomorrow. What is your pricing?")
        summaries_before = InternalConversationSummary.objects.filter(lead=self.lead).count()
        with patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=AssertionError("No model")):
            observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
            observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
        memory = StructuredLeadMemoryService().load(organization=self.organization, lead=self.lead)
        self.assertEqual(memory["facts"]["customer.budget"]["value"], "₹50000")
        self.assertEqual(len(memory["events"]), 1)
        self.assertEqual(memory["events"][0]["authority"], "customer_report")
        self.assertEqual(LeadSignal.objects.filter(lead=self.lead, kind="asked_pricing").count(), 1)
        self.assertEqual(InternalConversationSummary.objects.filter(lead=self.lead).count(), summaries_before)

    def test_memory_update_preserves_reported_events_and_canonical_attribute(self):
        AttributeDefinition.objects.create(organization=self.organization, name="Approved Budget", key="approved_budget", field_type="text")
        self.organization.settings = {"ai_memory": {"field_mappings": {"budget": "approved_budget"}}}
        self.organization.save(update_fields=["settings"])
        self.lead.attributes = {"approved_budget": "Manual approved budget"}
        self.lead.save(update_fields=["attributes"])
        source = self.source("My budget is ₹20. I will send details tomorrow.")
        observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
        next_source = self.source("We use Excel.", "intelligence-second")
        observe_accepted_turn(lead=self.lead, source_message_id=next_source.pk)
        memory = StructuredLeadMemoryService().load(organization=self.organization, lead=self.lead)
        self.assertEqual(memory["facts"]["approved_budget"]["value"], "Manual approved budget")
        self.assertEqual(len(memory["events"]), 1)
        self.assertEqual(memory["facts"]["customer.current_tools"]["value"], "Excel")

    def test_objections_are_saved_only_with_organization_permission(self):
        source = self.source("Too expensive")
        observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
        memory = StructuredLeadMemoryService().load(organization=self.organization, lead=self.lead)
        self.assertFalse(memory.get("events"))
        self.organization.settings = {"ai_objections": {"persist_memory": True}}
        self.organization.save(update_fields=["settings"])
        source = self.source("Too expensive", "intelligence-permitted")
        observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
        memory = StructuredLeadMemoryService().load(organization=self.organization, lead=self.lead)
        self.assertEqual(memory["events"][0]["kind"], "objection:PRICE_TOO_HIGH")
        self.assertTrue(LeadSignal.objects.filter(lead=self.lead, kind="negative_objection", detail="PRICE_TOO_HIGH").exists())

    def test_same_phone_in_other_organization_has_no_shared_memory_or_signals(self):
        source = self.source("My budget is ₹50. What is your pricing?")
        observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
        other = Organization.objects.create(name="Other organization")
        pipeline = Pipeline.objects.create(organization=other, name="Other pipeline")
        foreign = Lead.objects.create(organization=other, pipeline=pipeline, stage=pipeline.stages.first(), phone=self.lead.phone)
        self.assertFalse(StructuredLeadMemoryService().load(organization=other, lead=foreign)["facts"])
        self.assertEqual(signal_summary(organization=other, lead=foreign)["score"], 0)
        with self.assertRaises(TenantScopeError):
            signal_summary(organization=other, lead=self.lead)

    def test_signal_weights_can_change_without_rewriting_qualification_or_events(self):
        before_stage = self.lead.stage_id
        source = self.source("What is your pricing?")
        observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
        self.organization.settings = {"ai_signals": {"weights": {"asked_pricing": 3}}}
        result = signal_summary(organization=self.organization, lead=self.lead)
        self.assertEqual(result["score"], 3)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, before_stage)
        self.assertFalse(result["qualification_override"])

    def test_shared_accepted_hook_persists_intelligence_and_duplicate_hook_does_not(self):
        from apps.ai_engagement.services.engagement import EngagementDecision
        from apps.ai_engagement import tasks
        source = self.source("My budget is ₹50")
        decision = EngagementDecision(should_engage=True, message="Thanks for sharing that.",
            file_document_id=None, crm_actions=[], reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="test")
        with transaction.atomic():
            first = tasks._persist_engagement_answers(self.lead, decision, source.pk)
            second = tasks._persist_engagement_answers(self.lead, decision, source.pk)
        self.assertTrue(first)
        self.assertFalse(second)
        self.lead.refresh_from_db()
        self.assertIn(MEMORY_KEY, self.lead.attributes)
        self.assertEqual(LeadSignal.objects.filter(lead=self.lead, kind="supplied_budget").count(), 1)

    def test_three_accepted_turns_produce_repeated_engagement_once_per_source(self):
        for i in range(3):
            source = self.source("Hello", f"intelligence-repeat-{i}")
            observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
        self.assertEqual(LeadSignal.objects.filter(lead=self.lead, kind="repeated_engagement").count(), 1)

    def test_optional_enrichment_failure_does_not_poison_transaction(self):
        source = self.source("What is your pricing?")
        with transaction.atomic(), patch("apps.ai_engagement.services.lead_intelligence.analyze_turn", side_effect=RuntimeError("failed")):
            observe_accepted_turn(lead=self.lead, source_message_id=source.pk)
            self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())


class ResponseCompositionTests(SimpleTestCase):
    def payload(self):
        return {"organization": {"id": "org-a", "ai_profile": {"communication": {"languages": ["English", "Hindi"]}}},
                "lead": {"id": "lead-a", "name": "Asha", "qualification": {"engagement_mode": "qualification", "requirement_states": {}}},
                "conversation_policy": {"allowed_response_goal": "answer_customer_then_ask_next_qualification", "continue_qualification": True, "next_requirement_id": "tool"},
                "next_requirement": {"id": "tool", "question": "Which tool?\nA. Excel\nB. CRM", "options": [{"key": "A", "value": "Excel"}, {"key": "B", "value": "CRM"}]},
                "grounding": {"verified": True, "sensitive": True, "evidence": [{"source_id": "org:price", "content": "Price is 99."}]},
                "recent_conversation": {"messages": [{"direction": "outbound", "body": "Hello"}, {"direction": "inbound", "body": "price kya hai?"}]}}

    def test_plan_preserves_options_language_order_and_backend_authority(self):
        from apps.ai_engagement.services.response_composer import build_response_plan
        plan = build_response_plan(payload=self.payload(), organization_id="org-a", lead_id="lead-a",
            intent_decision=IntentDecision(primary_intent=Intent.PRICING_QUESTION, language="hinglish"), final_composition=True)
        self.assertEqual(plan.phase, "FINAL_COMPOSITION")
        self.assertEqual(plan.language, "hinglish")
        self.assertTrue(plan.already_greeted)
        self.assertEqual(plan.next_question, self.payload()["next_requirement"])
        self.assertEqual(plan.action_authority, "canonical_backend_only")
        self.assertNotIn("Price is 99", json.dumps(plan.trace_dict()))

    def test_foreign_scope_and_answered_question_fail_closed(self):
        from apps.ai_engagement.services.response_composer import build_response_plan
        with self.assertRaises(TenantScopeError):
            build_response_plan(payload=self.payload(), organization_id="org-b", lead_id="lead-a")
        payload = self.payload()
        payload["lead"]["qualification"]["requirement_states"] = {"tool": {"status": "answered"}}
        with self.assertRaises(ValueError):
            build_response_plan(payload=payload, organization_id="org-a", lead_id="lead-a")

    def test_forbidden_claim_cannot_bypass_full_grounding_chain(self):
        from apps.ai_engagement.graph.evidence import check_grounding
        from apps.ai_engagement.services.engagement import EngagementDecision
        decision = EngagementDecision(should_engage=True, message="Guaranteed returns!", file_document_id=None,
            crm_actions=[], reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="test")
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            result = check_grounding({"decision": decision, "organization": SimpleNamespace(id="org-a", settings={"ai_objections": {"forbidden_claims": ["guaranteed returns"]}}), "lead": SimpleNamespace(id="lead-a")})
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(result["grounding_validation_path"], "forbidden_claim")
        provider.assert_not_called()

    def test_trace_redacts_embedded_credentials_not_just_sensitive_keys(self):
        from apps.ai_engagement.services.trace_sanitizer import sanitize, preview
        text = "api_key=secretxyz Bearer abc.def.ghi sk-project0123456789"
        for value in (str(sanitize({"message": text})), preview(text)):
            self.assertNotIn("secretxyz", value)
            self.assertNotIn("abc.def.ghi", value)
            self.assertNotIn("sk-project0123456789", value)

    def test_turn_contexts_are_reset_even_when_processing_raises(self):
        from apps.ai_engagement.services.turn_scope import isolated_turn
        from apps.ai_engagement.services import conversation_policy_runtime as policy, intent_runtime
        sentinel = {"organization_id": "outer"}
        token = policy._TURN.set(sentinel)
        try:
            with self.assertRaises(RuntimeError):
                with isolated_turn():
                    self.assertIsNone(policy._TURN.get())
                    policy._TURN.set({"organization_id": "inner"})
                    intent_runtime._CURRENT.set({"organization_id": "inner"})
                    raise RuntimeError("failed turn")
            self.assertIs(policy._TURN.get(), sentinel)
            self.assertNotEqual(intent_runtime._CURRENT.get(), {"organization_id": "inner"})
        finally:
            policy._TURN.reset(token)
