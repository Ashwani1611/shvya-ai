"""Multi-intent file recovery uses current candidates, never a second action turn."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai_engagement.graph.workflow import _generate
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementDecision, EngagementService
from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY


class RequestedFileReviewTests(SimpleTestCase):
    def setUp(self):
        self.organization = SimpleNamespace(id="scoped-organization")
        self.lead = SimpleNamespace(id="scoped-lead")
        self.candidate = {"document_id": 18, "name": "Product brochure", "share_instruction":
                          "Send this product brochure along with welcome message or whenever the lead asks for the product brochure."}
        self.context = AIContext(
            organization={"id": self.organization.id, "_file_candidates": [self.candidate]},
            lead={"id": self.lead.id, "attributes": {"channel": "WhatsApp"}, "lead_source": "system"},
            pipeline={}, stage={"name": "New Lead"}, contacts=[], attributes=[],
            conversation={"messages": [{"id": "current", "direction": "inbound", "body":
                "My biggest problem is slow replies. I manage leads in WhatsApp, receive 20 leads per day, "
                "and I am not currently running paid ads. Please call me tomorrow at 3 PM India time "
                "and share the product brochure."}]},
            conversation_summary=None, qualification_notes=[], knowledge=[],
        )
        self.actions = [{"type": "create_reminder", "title": "Customer callback", "due_at": "2030-01-01T10:00:00+05:30"}]
        self.updates = [{"requirement_id": "problem", "value": "slow replies", "evidence": "slow replies"}]
        self.draft = EngagementDecision(
            should_engage=True, message="Your preferred callback time is noted.",
            file_document_id=None, crm_actions=self.actions, qualification_updates=self.updates,
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="recorded",
        )
        self.calls = []
        self.review = {"should_share": True, "document_id": 18, "reason": "The current request meets the authored condition."}
        self.provider = SimpleNamespace(generate_text=self.generate)
        self.service = EngagementService(provider=self.provider)

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return AITextResult(json.dumps(self.review), "recorded")

    def run_graph_generation(self, *, context=None, draft=None):
        context = context or self.context
        return _generate({
            "organization": self.organization, "lead": self.lead, "service": self.service,
            "context": context, "latest_text": context.conversation["messages"][-1]["body"],
            "turn_policy": SimpleNamespace(model_override="organization-response-model"),
            "legacy_engage": lambda *args, **kwargs: draft or self.draft,
        })["decision"]

    def test_combined_callback_and_brochure_repairs_only_the_missing_file(self):
        with patch("apps.ai_engagement.services.file_sharing.FileSharingService.get_eligible_documents", return_value=[object()]):
            decision = self.run_graph_generation()
        self.assertEqual(decision.file_document_id, 18)
        self.assertEqual(decision.crm_actions, self.actions)
        self.assertEqual(decision.qualification_updates, self.updates)
        self.assertEqual(decision.message, self.draft.message)
        self.assertEqual(len(self.calls), 1)
        review = self.calls[0]
        payload = json.loads(review["input_text"])
        self.assertEqual(payload["file_candidates"], [self.candidate])
        self.assertEqual(payload["stage"]["name"], "New Lead")
        self.assertEqual(review["metadata"]["model_override"], "organization-response-model")
        self.assertEqual(review["metadata"]["phase"], "file_selection_review")

    def test_reviewer_cannot_select_an_eligible_file_outside_current_candidate_allowlist(self):
        self.review["document_id"] = 99
        with patch("apps.ai_engagement.services.file_sharing.FileSharingService.get_eligible_documents", return_value=[object()]):
            self.assertIsNone(self.run_graph_generation().file_document_id)

    def test_foreign_or_now_ineligible_selection_is_rejected_without_losing_captures(self):
        with patch("apps.ai_engagement.services.file_sharing.FileSharingService.get_eligible_documents", return_value=[]):
            decision = self.run_graph_generation()
        self.assertIsNone(decision.file_document_id)
        self.assertEqual(decision.qualification_updates, self.updates)

    def test_current_request_does_not_override_authored_stage_restriction(self):
        self.candidate["share_instruction"] = "Send only after the lead reaches Qualified."
        self.review = {"should_share": False, "document_id": None, "reason": "The current stage is New Lead, not Qualified."}
        decision = self.run_graph_generation()
        self.assertIsNone(decision.file_document_id)
        payload = json.loads(self.calls[0]["input_text"])
        self.assertEqual(payload["stage"]["name"], "New Lead")
        self.assertIn("only after", payload["file_candidates"][0]["share_instruction"])

    def test_malformed_review_cannot_change_reply_actions_or_file_choice(self):
        self.review = {"should_share": True, "document_id": True, "reason": "invalid Boolean ID"}
        decision = self.run_graph_generation()
        self.assertEqual(decision, self.draft)

    def test_preview_final_language_pass_cannot_reconsider_file_selection(self):
        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            self.assertEqual(self.run_graph_generation(), self.draft)
        finally:
            _FINAL_LANGUAGE_ONLY.reset(token)
        self.assertEqual(self.calls, [])

    def test_welcome_guidance_selected_on_first_greeting_is_preserved_without_review(self):
        context = replace(self.context, conversation={"messages": [{"id": "welcome", "direction": "inbound", "body": "Hello"}]})
        decision = self.run_graph_generation(context=context, draft=replace(self.draft, file_document_id=18))
        self.assertEqual(decision.file_document_id, 18)
        self.assertEqual(self.calls, [])

    def test_first_greeting_reviews_authored_welcome_guidance_without_changing_other_decisions(self):
        context = replace(self.context, conversation={"message_count": 1, "messages": [{"id": "welcome", "direction": "inbound", "body": "Hi"}]})
        with patch("apps.ai_engagement.services.file_sharing.FileSharingService.get_eligible_documents", return_value=[object()]):
            decision = self.run_graph_generation(context=context)
        self.assertEqual(decision.file_document_id, 18)
        self.assertEqual(decision.message, self.draft.message)
        self.assertEqual(decision.qualification_updates, self.updates)
        self.assertTrue(json.loads(self.calls[0]["input_text"])["welcome_due"])
        self.assertIn("allowed only on explicit request must not be selected for a greeting", self.calls[0]["instructions"])

    def test_first_greeting_does_not_force_request_only_file_selection(self):
        self.candidate["share_instruction"] = "Send only when the lead explicitly requests the product brochure."
        self.review = {"should_share": False, "document_id": None, "reason": "A greeting is not an explicit request."}
        context = replace(self.context, conversation={"messages": [{"id": "welcome", "direction": "inbound", "body": "Hi"}]})
        decision = self.run_graph_generation(context=context)
        self.assertIsNone(decision.file_document_id)
        self.assertTrue(json.loads(self.calls[0]["input_text"])["welcome_due"])

    def test_scoped_live_context_cannot_claim_welcome_when_canonical_predicate_rejects_it(self):
        from apps.crm.models import Lead
        self.lead = Lead()
        context = replace(self.context, conversation={"messages": [{"id": "welcome", "direction": "inbound", "body": "Hi"}]})
        with patch("apps.ai_engagement.services.first_inbound_welcome_runtime._is_first_inbound_turn", return_value=False) as first_turn:
            self.assertEqual(self.run_graph_generation(context=context), self.draft)
        first_turn.assert_called_once_with(self.lead)
        self.assertEqual(self.calls, [])

    def test_first_information_answer_does_not_claim_a_welcome_attachment(self):
        context = replace(self.context, stage={"name": "Qualified"}, conversation={"messages": [{"id": "info", "direction": "inbound", "body": "What is your price?"}]})
        draft = replace(self.draft, reason_code="ANSWER_ORG_QUESTION")
        self.assertEqual(self.run_graph_generation(context=context, draft=draft), draft)
        self.assertEqual(self.calls, [])

    def test_unrequested_guided_file_on_later_turn_does_not_trigger_extra_repair_model_call(self):
        context = replace(self.context, conversation={"messages": [
            {"id": "reply", "direction": "outbound", "body": "How can I help?"},
            {"id": "plain", "direction": "inbound", "body": "Thanks"},
        ]})
        self.assertEqual(self.run_graph_generation(context=context), self.draft)
        self.assertEqual(self.calls, [])


@override_settings(OPENAI_API_KEY="unit-test-unused-key", AI_BRAIN_RECOVERY_ENABLED=False)
class SandboxRequestedFileIntegrationTests(TestCase):
    """Real graph/preview boundaries; no provider network calls or customer sends."""
    def setUp(self):
        from apps.ai_engagement.models import Document, OrgInfo
        from apps.ai_engagement.services.embeddings import EmbeddingError
        from apps.crm.models import Pipeline, Stage
        from apps.organizations.models import Organization
        cache.clear()
        self.addCleanup(cache.clear)
        self.organization = Organization.objects.create(name="Combined preview company")
        self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales")
        self.new_lead, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, name="New leads", defaults={"display_order": 1},
        )
        last_order = Stage.objects.filter(pipeline=self.pipeline).order_by("-display_order").values_list("display_order", flat=True).first()
        self.call_stage, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, name="Call Requested", defaults={"display_order": (last_order or 0) + 1},
        )
        self.info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        self.info.about = "We automate customer conversations."
        self.info.bot_languages = "English, Hinglish"
        self.info.ai_playbook = (
            "## Stage shifting logic\nWhen the customer requests a call, move to Call Requested.\n"
            "## Reminder creation logic\nReminder 1: Customer Callback\n"
            "- Create when the customer explicitly requests a callback and provides or confirms a future date and time.\n"
            "- Title: Customer Callback.\n"
            "- Due time: The customer-agreed future date and time.\n"
            "- Time zone: Asia/Kolkata unless the customer explicitly specifies another time zone.\n"
            "- Notes: Record the factual callback purpose and requested timing.\n"
            "- These are internal CRM reminders, not confirmed calls or appointments."
        )
        self.info.save()
        self.document = Document.objects.create(
            organization=self.organization, name="Product brochure", file="knowledge/brochure.pdf",
            processing_status="completed", is_active=True,
            share_instruction="Send this product brochure along with welcome message or whenever the lead asks for the product brochure.",
        )
        self.phases = []
        self.grounding_inputs = []
        self.review_should_share = True
        self.greeting_requirement = None
        self.reply = "We have a product brochure. I will now proceed to set up the call and share the product brochure with you."
        for patcher in (
            patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=self.provider),
            patch("apps.ai_engagement.services.embeddings.EmbeddingService._get_client", side_effect=EmbeddingError("no live embeddings")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def provider(self, **kwargs):
        phase = kwargs.get("metadata", {}).get("phase")
        self.phases.append(phase)
        if phase == "grounding":
            self.grounding_inputs.append(json.loads(kwargs["input_text"]))
            return AITextResult('{"approved":true,"reason":"approved"}', "recorded")
        if phase == "intent_classification":
            return AITextResult(json.dumps({
                "primary_intent": "GREETING" if self.greeting_requirement else "CALL_REQUEST", "secondary_intents": [], "confidence": 0.9,
                "entities": [], "facts": [], "direct_question": None, "qualification_candidate": None,
                "requested_action": None, "language": "English", "requires_knowledge": False, "requires_human": not bool(self.greeting_requirement),
            }), "recorded")
        if phase == "file_selection_review":
            return AITextResult(json.dumps({"should_share": self.review_should_share,
                                            "document_id": self.document.pk if self.review_should_share else None,
                                            "reason": "The request satisfies the authored guidance."}), "recorded")
        if self.greeting_requirement:
            return AITextResult(json.dumps({
                "should_engage": True, "silence_rule": None,
                "message": self.greeting_requirement["question"], "file_document_id": None,
                "qualification_updates": [], "next_requirement_id": self.greeting_requirement["id"],
                "crm_actions": [], "reason_code": "QUALIFICATION_NEXT",
            }), "recorded")
        return AITextResult(json.dumps({
            "should_engage": True, "silence_rule": None, "message": self.reply,
            "file_document_id": None, "qualification_updates": [], "next_requirement_id": None,
            "crm_actions": [{"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.call_stage.pk)}},
                            {"type": "create_reminder", "title": "Customer Callback", "description": "Requested callback",
                             "due_at": "2030-01-01T15:00:00+05:30"}],
            "reason_code": "NORMAL_CONVERSATION",
        }), "recorded")

    def run_turn(self, session="mixed"):
        from apps.ai_engagement.services.playground import PlaygroundService
        return PlaygroundService().run(
            organization=self.organization, session_id=session, stage_id=str(self.new_lead.pk),
            message="My biggest problem is slow replies. I manage leads in WhatsApp, receive 20 leads per day, "
                    "and I am not currently running paid ads. Please call me on 1 January 2030 at 3 PM India time "
                    "and share the product brochure.",
        )

    def assert_preview_only(self, result):
        from apps.channels.models import WhatsAppMessage
        from apps.crm.models import Lead, LeadActivity, LeadNote, LeadReminder
        self.assertEqual([item["id"] for item in result.files], [self.document.pk])
        reminders = [item for item in result.events if item["type"] == "reminder"]
        self.assertEqual(len(reminders), 1, result.events)
        self.assertEqual(reminders[0]["status"], "preview")
        self.assertEqual(reminders[0]["title"], "Customer Callback")
        self.assertEqual(reminders[0]["due_at"], "2030-01-01T15:00:00+05:30")
        self.assertEqual(result.stage["name"], "Call Requested")
        self.assertIn("We have a product brochure.", result.response)
        self.assertNotIn("I will now proceed", result.response)
        self.assertIn("no live call is confirmed", result.response)
        for model in (Lead, LeadActivity, LeadNote, LeadReminder, WhatsAppMessage):
            self.assertEqual(model.objects.count(), 0, model.__name__)

    def test_omitted_file_recovers_before_final_language_composition(self):
        result = self.run_turn()
        self.assertEqual(self.phases.count("file_selection_review"), 1)
        self.assertEqual(self.phases.count("primary"), 2)
        self.assert_preview_only(result)

    def test_final_language_pass_cannot_drop_preview_file_or_promise_live_callback(self):
        result = self.run_turn("finalized")
        self.assertEqual(self.phases.count("file_selection_review"), 1)
        self.assert_preview_only(result)

    def test_sparse_actual_reply_declined_file_review_is_visible_and_never_promises_a_callback(self):
        self.review_should_share = False
        self.reply = (
            "Thank you for your message. I've noted your request for a call on 1 January 2030 "
            "at 3 PM India time and your interest in the product brochure. "
            "Our team will be in touch to confirm the details."
        )
        result = self.run_turn("sparse-declined")
        self.assertEqual(result.files, [])
        self.assertEqual(result.stage["name"], "Call Requested")
        self.assertTrue(any(item["type"] == "reminder" for item in result.events))
        self.assertIn("I've noted your request", result.response)
        self.assertNotIn("will be in touch", result.response)
        self.assertIn("file/draft/candidates=1/review=declined", result.diagnostics)
        self.assertIn("file/preview=0", result.diagnostics)
        from apps.channels.models import WhatsAppMessage
        from apps.crm.models import Lead, LeadActivity, LeadNote, LeadReminder
        for model in (Lead, LeadActivity, LeadNote, LeadReminder, WhatsAppMessage):
            self.assertEqual(model.objects.count(), 0, model.__name__)

    def run_greeting_turn(self, session):
        from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
        from apps.ai_engagement.services.playground import PlaygroundService
        question = (
            "Q1. What is your biggest challenge with managing or converting leads right now? "
            "A. Slow replies B. Missed follow-ups C. Leads going cold D. No proper tracking"
        )
        self.greeting_requirement = compile_qualification_requirements(question)["requirements"][0]
        self.info.ai_playbook += (
            "\n## Welcome Message\nHi, I'm Ria. Welcome to Shvya AI.\n"
            "## Qualification Questions\n" + question
        )
        self.info.save()
        return PlaygroundService().run(
            organization=self.organization, session_id=session, stage_id=str(self.new_lead.pk), message="Hi",
        )

    def assert_greeting_only_preview(self, result):
        from apps.channels.models import WhatsAppMessage
        from apps.crm.models import Lead, LeadActivity, LeadNote, LeadReminder
        self.assertIn("Hi, I'm Ria. Welcome to Shvya AI.", result.response)
        self.assertIn(self.greeting_requirement["question"], result.response)
        for option in ("Slow replies", "Missed follow-ups", "Leads going cold", "No proper tracking"):
            self.assertIn(option, result.response)
        self.assertEqual(result.stage["name"], "New leads")
        self.assertFalse(any(item["type"] in {"attribute_updates", "stage_transition", "reminder"} for item in result.events), result.events)
        for model in (Lead, LeadActivity, LeadNote, LeadReminder, WhatsAppMessage):
            self.assertEqual(model.objects.count(), 0, model.__name__)

    def test_authored_welcome_guidance_recovers_brochure_while_preserving_q1_and_options(self):
        result = self.run_greeting_turn("welcome-file")
        self.assertEqual([item["id"] for item in result.files], [self.document.pk])
        self.assertEqual(self.phases.count("file_selection_review"), 1)
        self.assertIn("trigger=welcome", result.diagnostics)
        self.assertIn("review=selected", result.diagnostics)
        selected_grounding = [item for item in self.grounding_inputs if item.get("selected_file_document_id") == self.document.pk]
        self.assertTrue(selected_grounding, self.phases)
        self.assertTrue(selected_grounding[0]["welcome_due"])
        self.assertNotIn("Welcome to Shvya AI", selected_grounding[0]["reply"])
        self.assert_greeting_only_preview(result)

    def test_greeting_does_not_select_brochure_authorized_only_on_explicit_request(self):
        self.document.share_instruction = "Send only when the lead explicitly requests the product brochure."
        self.document.save()
        self.review_should_share = False
        result = self.run_greeting_turn("welcome-request-only")
        self.assertEqual(result.files, [])
        self.assertEqual(self.phases.count("file_selection_review"), 1)
        self.assertIn("trigger=welcome", result.diagnostics)
        self.assertIn("review=declined", result.diagnostics)
        self.assert_greeting_only_preview(result)

    def test_unknown_configured_language_cannot_save_empty_successful_reply(self):
        from apps.ai_engagement.services.playground import PlaygroundError, PlaygroundService
        self.info.bot_languages = "German"
        self.info.save()
        self.reply = "I will send the brochure."
        with (
            patch("apps.ai_engagement.services.playground.apply_first_inbound_welcome", side_effect=lambda **kwargs: kwargs["decision"]),
        ):
            with self.assertRaises(PlaygroundError):
                self.run_turn("unsupported-safe-copy")
        self.assertFalse(PlaygroundService()._load_session_payload(organization=self.organization, session_id="unsupported-safe-copy").get("history"))
