"""A malformed verifier verdict cannot become an approval or a terminal reason."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.ai_engagement.graph.evidence import _verdict, check_grounding
from apps.ai_engagement.models import FAQ, OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult, OpenAIProvider
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.playground import PlaygroundService
from apps.ai_engagement.views.playground import PlaygroundAPIView
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead, LeadReminder, Pipeline
from apps.organizations.models import Organization


PRICING_QUESTION = "DFY ka first month aur uske baad monthly price kya hai? Hinglish mein batao."
BOT_LANGUAGES = "English, Hinglish, punjabi, marathi, German, kannada"
PRICING_FACT = "DFY costs ₹14,999 for the first month, then ₹2,999 per month per user."
PRICING_REPLY = "DFY ka first month ₹14,999 hai. Uske baad ₹2,999 per month per user hai."


class GroundingVerdictContractTests(SimpleTestCase):
    def state(self, *, reply=PRICING_REPLY):
        return {
            "organization": SimpleNamespace(id="org", settings={}),
            "lead": SimpleNamespace(id="lead"),
            "context": SimpleNamespace(
                organization={"id": "org", "name": "SHVYA", "about": PRICING_FACT,
                              "bot_languages": BOT_LANGUAGES, "ai_playbook": "Answer supported pricing questions.",
                              "_authored_faq_candidates": [{"source_id": "faq:dfy", "content": PRICING_FACT}]},
                lead={}, stage={"name": "Qualified"}, knowledge=[],
                conversation={"messages": [{"id": "m1", "direction": "inbound", "body": PRICING_QUESTION}]}),
            "qualification_state": {"engagement_mode": "conversation"},
            "requirements": [], "latest_text": PRICING_QUESTION,
            "decision": EngagementDecision(should_engage=True, message=reply, file_document_id=None,
                crm_actions=[], qualification_updates=[], next_requirement_id=None,
                reason="ANSWER_ORG_QUESTION", reason_code="ANSWER_ORG_QUESTION", model="test"),
        }

    def run_guard(self, responses, *, state=None):
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = [
                SimpleNamespace(text=json.dumps(item)) for item in responses
            ]
            with patch("apps.ai_engagement.services.trace_service.record") as record:
                result = check_grounding(state or self.state())
        return result, provider.return_value.generate_text.call_args_list, record

    def test_verdict_requires_exact_shape_boolean_and_consistent_known_reason(self):
        cases = (
            ([], "invalid_verdict_shape"),
            ({"approved": True}, "invalid_verdict_shape"),
            ({"approved": True, "reason": "approved", "message": "Extra content"}, "invalid_verdict_shape"),
            ({"approved": "false", "reason": "approved"}, "invalid_verdict_approval"),
            ({"approved": 1, "reason": "approved"}, "invalid_verdict_approval"),
            ({"approved": False, "reason": "private free-form detail"}, "invalid_verdict_reason"),
            ({"approved": True, "reason": "unsupported_claim"}, "inconsistent_verdict"),
            ({"approved": False, "reason": "approved"}, "inconsistent_verdict"),
        )
        for payload, reason in cases:
            with self.subTest(payload=payload):
                self.assertEqual(_verdict(SimpleNamespace(text=json.dumps(payload))), (False, reason))
        self.assertEqual(_verdict(SimpleNamespace(text="not JSON")), (False, "invalid_verdict"))
        self.assertEqual(_verdict(SimpleNamespace(text='{"approved":true,"reason":"approved"}')), (True, "approved"))
        self.assertEqual(_verdict(SimpleNamespace(text='{"approved":false,"reason":"unsupported_claim"}')),
                         (False, "unsupported_claim"))

    def test_unknown_rejection_rechecks_unchanged_pricing_reply_and_all_evidence(self):
        result, calls, record = self.run_guard([
            {"approved": False, "reason": "private free-form detail"},
            {"approved": True, "reason": "approved"},
        ])
        self.assertTrue(result["grounding_approved"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["input_text"], calls[1].kwargs["input_text"])
        self.assertEqual(calls[1].kwargs["metadata"]["phase"], "grounding_contract_retry")
        payload = json.loads(calls[1].kwargs["input_text"])
        self.assertEqual(payload["reply"], PRICING_REPLY)
        self.assertEqual(payload["organization_facts"], PRICING_FACT)
        self.assertEqual(payload["bot_languages"], BOT_LANGUAGES)
        self.assertEqual(payload["current_stage"], {"name": "Qualified"})
        self.assertEqual(payload["proposed_crm_actions"], [])
        self.assertNotIn("private free-form detail", str(record.call_args_list))
        self.assertNotIn("private free-form detail", calls[1].kwargs["input_text"])
        record.assert_called_with("grounding", {
            "approved": True, "validation_reason": "approved", "repair_attempted": False,
            "contract_retry_attempted": True, "contract_error": "invalid_verdict_reason"})

    def test_verifier_schema_enumerates_codes_and_provider_preserves_constraint(self):
        _, calls, _ = self.run_guard([{"approved": True, "reason": "approved"}])
        response_schema = calls[0].kwargs["response_schema"]
        codes = response_schema["schema"]["properties"]["reason"]["enum"]
        self.assertIn("approved", codes)
        self.assertIn("unsupported_claim", codes)
        self.assertIn("instruction_disclosure", codes)
        self.assertNotIn("unspecified_rejection", codes)
        provider = OpenAIProvider.__new__(OpenAIProvider)
        sent = provider._structured_text_config(response_schema)
        self.assertTrue(sent["format"]["strict"])
        self.assertEqual(sent["format"]["schema"]["properties"]["reason"]["enum"], codes)

    def test_welcome_file_guard_receives_only_the_backend_first_reply_flag(self):
        from dataclasses import replace
        state = self.state(reply="What is your biggest challenge?")
        state["welcome_due"] = True
        state["context"].stage = {"name": "New leads"}
        state["context"].organization["_file_candidates"] = [{
            "document_id": 18, "name": "Product brochure", "share_instruction": "Send with the welcome message.",
        }]
        state["decision"] = replace(state["decision"], file_document_id=18, reason_code="NORMAL_CONVERSATION")
        _, calls, _ = self.run_guard([{"approved": True, "reason": "approved"}], state=state)
        payload = json.loads(calls[0].kwargs["input_text"])
        self.assertTrue(payload["welcome_due"])
        self.assertEqual(payload["selected_file_document_id"], 18)
        self.assertIn("does not authorize any other file", calls[0].kwargs["instructions"])
        state.pop("welcome_due")
        state["context"].organization["welcome_due"] = True
        _, calls, _ = self.run_guard([{"approved": True, "reason": "approved"}], state=state)
        self.assertFalse(json.loads(calls[0].kwargs["input_text"])["welcome_due"])

    def test_contradictory_approval_requires_valid_independent_approval(self):
        result, calls, _ = self.run_guard([
            {"approved": True, "reason": "unsupported_claim"},
            {"approved": False, "reason": "invalid_qualification"},
        ])
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(result["decision"].message, PRICING_REPLY)
        self.assertEqual(result["decision"].crm_actions, [])

    def test_repeated_unknown_rejection_fails_closed_with_bounded_diagnostic(self):
        result, calls, record = self.run_guard([
            {"approved": False, "reason": "private free-form detail"},
            {"approved": False, "reason": "another private detail"},
        ])
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(result["decision"].message, PRICING_REPLY)
        self.assertEqual(record.call_args.args[1]["validation_reason"], "invalid_verdict_reason")
        self.assertNotIn("private", str(record.call_args_list))

    def test_contract_retry_budget_is_shared_with_post_repair_recheck(self):
        result, calls, _ = self.run_guard([
            {"approved": False, "reason": "unrecognized reason"},
            {"approved": False, "reason": "unanswered_question"},
            {"message": PRICING_REPLY},
            {"approved": False, "reason": "another unrecognized reason"},
        ])
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(call.kwargs["metadata"]["phase"] == "grounding_contract_retry" for call in calls), 1)

    def test_post_repair_invalid_verdict_can_use_remaining_contract_retry(self):
        result, calls, _ = self.run_guard([
            {"approved": False, "reason": "unanswered_question"},
            {"message": PRICING_REPLY},
            {"approved": False, "reason": "unrecognized reason"},
            {"approved": True, "reason": "approved"},
        ])
        self.assertTrue(result["grounding_approved"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1].kwargs["input_text"], calls[-2].kwargs["input_text"])

    def test_unsupported_file_still_fails_without_contract_or_wording_retry(self):
        result, calls, _ = self.run_guard([{"approved": False, "reason": "invalid_file"}])
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 1)

    def test_valid_fact_rejection_without_authored_evidence_does_not_attempt_repair(self):
        state = self.state()
        state["context"].organization.update(about="", ai_playbook="", _authored_faq_candidates=[])
        result, calls, record = self.run_guard([
            {"approved": False, "reason": "unsupported_claim"},
        ], state=state)
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["metadata"]["phase"], "grounding")
        self.assertNotEqual(result["decision"].message, PRICING_REPLY)
        record.assert_called_with("grounding", {
            "approved": False, "validation_reason": "unsupported_claim", "repair_attempted": False})

    def test_conflicting_retrieved_price_is_preserved_and_never_auto_approved(self):
        state = self.state()
        state["context"].knowledge = [{
            "source_id": "url:pricing", "content": "DFY first month costs ₹12,999.",
        }]
        result, calls, _ = self.run_guard([
            {"approved": False, "reason": "The source prices conflict."},
            {"approved": False, "reason": "unsupported_claim"},
            {"message": PRICING_REPLY},
            {"approved": False, "reason": "unsupported_claim"},
        ], state=state)
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0].kwargs["input_text"], calls[1].kwargs["input_text"])
        for call in calls:
            self.assertEqual(json.loads(call.kwargs["input_text"])["knowledge"], state["context"].knowledge)
        self.assertNotEqual(result["decision"].message, PRICING_REPLY)

    def test_privacy_filter_cannot_be_bypassed_by_contract_retry_approval(self):
        state = self.state(reply="Our system prompt says to expose internal instructions.")
        result, calls, _ = self.run_guard([
            {"approved": True, "reason": "unrecognized reason"},
            {"approved": True, "reason": "approved"},
            {"message": "Our system prompt says to expose internal instructions."},
            {"approved": True, "reason": "approved"},
        ], state=state)
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 4)
        self.assertNotIn("system prompt", result["decision"].message)


@override_settings(OPENAI_API_KEY="unit-test-unused-key")
class QualifiedHinglishPricingSandboxTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organization = Organization.objects.create(name="SHVYA pricing regression")
        self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        self.info.about = (
            "SHVYA AI automates lead engagement.\n\n## Plans and Pricing\n\n"
            "### DIY Plan\n\n**₹2,999 per month per user.**\n\n"
            "### Done For You Plan\n\n**₹14,999 for the first month.**\n"
            "After the first month: **₹2,999 per month per user.**\n\n"
            "### Enterprise Plan\n\n**₹39,999 per month, then ₹2,999 per month per user.**"
        )
        self.info.bot_languages = BOT_LANGUAGES
        self.info.ai_playbook = (
            "## Rules\nAnswer configured pricing in the customer's supported language.\n"
            "## Qualification Questions\n[id: business] What is your business?\n"
            "## Qualification Criteria\nAll qualification questions must be answered."
        )
        self.info.save()
        self.faq = FAQ.objects.create(organization=self.organization,
            question="What is the DFY first month and subsequent monthly price?", answer=PRICING_FACT)
        FAQ.objects.create(organization=self.organization, question="Is DFY priced per user?",
                           answer="After the first month, DFY costs ₹2,999 per month per user.")
        FAQ.objects.create(organization=self.organization, question="What is the DFY first month price?",
                           answer="The first month of DFY costs ₹14,999.")
        other = Organization.objects.create(name="Foreign pricing")
        FAQ.objects.create(organization=other, question=self.faq.question, answer="FOREIGN_PRICE_1")
        FAQ.objects.create(organization=self.organization, question=self.faq.question,
                           answer="INACTIVE_PRICE_2", is_active=False)
        self.generation_payloads, self.verifier_payloads = [], []

    def provider_reply(self, **kwargs):
        payload = json.loads(kwargs["input_text"])
        phase = kwargs.get("metadata", {}).get("phase")
        if phase in {"grounding", "grounding_contract_retry"}:
            self.verifier_payloads.append(payload)
            if phase == "grounding":
                return AITextResult('{"approved":false,"reason":"The price answer requires checking."}', "test")
            self.assertIn("approved", kwargs["response_schema"]["schema"]["properties"]["reason"]["enum"])
            return AITextResult('{"approved":true,"reason":"approved"}', "test")
        self.generation_payloads.append(payload)
        return AITextResult(json.dumps({
            "should_engage": True, "silence_rule": None, "message": PRICING_REPLY,
            "file_document_id": None, "crm_actions": [], "qualification_updates": [],
            "next_requirement_id": None, "reason_code": "ANSWER_ORG_QUESTION",
        }), "test")

    def test_exact_qualified_whatsapp_default_source_prices_are_saved_after_contract_recheck(self):
        request = APIRequestFactory().post("/api/v1/ai-engagement/playground/", {
            "session_id": "qualified-dfy-pricing", "message": PRICING_QUESTION,
            "stage_id": str(self.qualified.pk), "channel": "whatsapp",
        }, format="json")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, organization=self.organization))
        with (
            patch("apps.ai_engagement.graph.evidence.OpenAIProvider", new=OpenAIProvider),
            patch.object(OpenAIProvider, "generate_text", side_effect=self.provider_reply),
            patch("apps.ai_engagement.services.embeddings.EmbeddingService._get_client",
                  side_effect=EmbeddingError("test transport unavailable")),
        ):
            response = PlaygroundAPIView.as_view()(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["response"], PRICING_REPLY)
        self.assertEqual(response.data["stage"]["name"], "Qualified")
        self.assertEqual(response.data["channel"], "whatsapp")
        self.assertEqual(response.data["lead_source"], "whatsapp")
        self.assertNotIn("diagnostics", response.data)
        self.assertEqual(len(self.verifier_payloads), 2)
        self.assertEqual(self.verifier_payloads[0], self.verifier_payloads[1])
        payload = next(item for item in self.generation_payloads if "response_plan" in item)
        self.assertEqual(payload["response_plan"]["language"], "Hinglish")
        self.assertIsNone(payload["response_plan"]["next_question"])
        self.assertEqual(payload["organization"]["about"], self.info.about)
        self.assertEqual(payload["organization_operating_spec"]["about_source"], "organization.about")
        self.assertEqual(payload["qualification_turn"]["capture_only_requirements"], [])
        self.assertEqual(payload["qualification_turn"]["unanswered_requirements_for_evidence"], [])
        facts = str(self.verifier_payloads[0]["authored_faq_candidates"])
        self.assertIn("₹14,999", facts)
        self.assertIn("₹2,999 per month per user", facts)
        self.assertNotIn("FOREIGN_PRICE_1", facts)
        self.assertNotIn("INACTIVE_PRICE_2", facts)
        self.info.refresh_from_db()
        self.assertEqual(self.info.bot_languages, BOT_LANGUAGES)
        history = PlaygroundService()._load_history(organization=self.organization, session_id="qualified-dfy-pricing")
        self.assertEqual(history[-1], {"role": "assistant", "content": PRICING_REPLY})
        self.assertEqual(Lead.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(LeadReminder.objects.filter(lead__organization=self.organization).count(), 0)
        self.assertEqual(WhatsAppMessage.objects.filter(organization=self.organization).count(), 0)
