"""A sparse first draft cannot silently omit explicit authored qualification facts."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai_engagement.models import AIActionReceipt, Document, OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult, OpenAIProvider
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.engagement import EngagementDecision, EngagementService
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
from apps.ai_engagement.services.playground import PlaygroundService, _SandboxLead
from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY
from apps.ai_engagement.services.qualification_capture_recovery import recover_omitted_answers
from apps.ai_engagement.services.qualification_state import QUALIFICATION_STATE_KEY, state_for_lead
from apps.ai_engagement.tests import test_first_turn_volunteered_capture as authored_fixtures
from apps.ai_engagement.tests import test_precise_orchestration as context_fixtures
from apps.crm.models import AttributeDefinition, Lead, LeadReminder, Pipeline, Stage
from apps.organizations.models import Organization
from tests.playbook_fixtures import build_ai_playbook


_SOURCE = (
    "My biggest problem is slow replies. I manage leads in WhatsApp, receive 20 leads per day, "
    "and I am not currently running paid ads."
)


def _draft(updates=(), **overrides):
    data = dict(should_engage=True, message="Thanks for sharing your setup.", file_document_id=None,
                crm_actions=[], qualification_updates=list(updates), next_requirement_id=None,
                reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="recorded-draft")
    data.update(overrides)
    return EngagementDecision(**data)


def _answers(requirements, source_id, *, instagram=False):
    values = ("Missed follow-ups" if instagram else "Slow replies",
              "Excel / Google Sheets" if instagram else "WhatsApp", "11–30", "No")
    quotes = ("missed follow-ups" if instagram else "slow replies",
              "Google Sheets" if instagram else "WhatsApp", "20 leads per day", "I am not currently running paid ads")
    return [{"requirement_id": goal["id"], "value": value, "source_message_id": source_id, "evidence": quote}
            for goal, value, quote in zip(requirements, values, quotes, strict=True)]


@override_settings(OPENAI_API_KEY="unit-test-unused-key",
                   CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class CaptureReviewBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.context = context_fixtures.PreciseEngagementTests()._context(_SOURCE)
        self.context.organization["ai_playbook"] = build_ai_playbook(questions=authored_fixtures._QUESTIONS)
        self.context.stage.update(name="New leads")
        self.context.conversation["messages"][0]["id"] = "source-current"
        self.org, self.lead = SimpleNamespace(id="org-1"), SimpleNamespace(id="lead-1")
        self.requirements = compile_org_ai_profile_from_context(self.context.organization)["qualification"]["requirements"]
        self.state = state_for_lead(SimpleNamespace(attributes={}, stage=SimpleNamespace(name="New leads")),
                                    requirements=self.requirements)
        self.updates = _answers(self.requirements, "source-current")
        self.provider = Mock()
        self.provider.generate_text.return_value = AITextResult(json.dumps({"qualification_updates": self.updates}), "review")
        self.service = EngagementService(provider=self.provider)

    def recover(self, draft=None, **kwargs):
        return recover_omitted_answers(service=self.service, organization=self.org, lead=self.lead,
            context=kwargs.pop("context", self.context), decision=draft or _draft(),
            requirements=self.requirements, qualification_state=kwargs.pop("state", self.state), **kwargs)

    def test_omitted_four_answers_recover_using_authored_goals_and_current_source(self):
        recovered = self.recover()
        self.assertEqual(recovered.qualification_updates, self.updates)
        call = self.provider.generate_text.call_args.kwargs
        self.assertEqual(call["metadata"]["phase"], "qualification_capture_recovery")
        supplied = json.loads(call["input_text"])
        self.assertEqual(supplied["current_inbound"], {"id": "source-current", "body": _SOURCE})
        self.assertEqual([item["id"] for item in supplied["unanswered_authored_goals"]],
                         [item["id"] for item in self.requirements])

    def test_partial_draft_does_not_duplicate_answers_or_clobber_request_actions(self):
        self.provider.generate_text.return_value = AITextResult(json.dumps({"qualification_updates": self.updates[1:]}), "review")
        actions = [{"type": "pipeline_transition", "stage_shift": {"stage_id": "requested-call"}}]
        recovered = self.recover(_draft(self.updates[:1], crm_actions=actions, file_document_id=9))
        self.assertEqual(recovered.qualification_updates, self.updates)
        self.assertEqual(recovered.crm_actions, actions)
        self.assertEqual(recovered.file_document_id, 9)

    def test_trusted_earlier_answer_survives_current_source_only_recovery(self):
        prior = {**self.updates[0], "source_message_id": "older-inbound"}
        self.context.conversation["messages"].insert(0, {"id": "older-inbound", "direction": "inbound", "body": "My issue is slow replies."})
        self.provider.generate_text.return_value = AITextResult(json.dumps({"qualification_updates": self.updates[1:]}), "review")
        recovered = self.recover(_draft([prior]))
        self.assertEqual(recovered.qualification_updates, [prior, *self.updates[1:]])

    def test_hinglish_facts_use_semantic_review_without_english_answer_phrases(self):
        body = "Mere follow-ups miss hote hain. Leads Google Sheets mein manage karta hoon, roz 20 leads aate hain, paid ads nahi chala raha."
        updates = _answers(self.requirements, "source-current", instagram=True)
        for update, quote in zip(updates, ("follow-ups miss hote hain", "Google Sheets", "roz 20 leads", "paid ads nahi chala raha"), strict=True):
            update["evidence"] = quote
        self.provider.generate_text.return_value = AITextResult(json.dumps({"qualification_updates": updates}), "review")
        context = replace(self.context, conversation={"messages": [{"id": "source-current", "direction": "inbound", "body": body}]})
        self.assertEqual(self.recover(context=context).qualification_updates, updates)

    def test_multilingual_clause_review_does_not_require_english_keyword_hints(self):
        body = "Meine größte Schwierigkeit sind langsame Antworten. Ich verwalte meine Anfragen in einer Tabelle, täglich erhalte ich zwanzig Anfragen, bezahlte Werbung nutze ich aktuell nicht."
        self.provider.generate_text.return_value = AITextResult('{"qualification_updates":[]}', "review")
        context = replace(self.context, conversation={"messages": [{"id": "source-current", "direction": "inbound", "body": body}]})
        self.recover(context=context)
        self.provider.generate_text.assert_called_once()

    def test_final_language_pass_leaves_draft_capture_counters_untouched(self):
        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            with patch("apps.ai_engagement.services.qualification_capture_recovery._record") as record:
                self.recover()
            record.assert_not_called()
        finally:
            _FINAL_LANGUAGE_ONLY.reset(token)

    def test_wrong_goal_source_option_or_quote_is_rejected_without_mutating_draft(self):
        for field, value in (("requirement_id", "unknown-goal"), ("source_message_id", "older-source"),
                             ("value", "Invented option"), ("evidence", "not in this customer message")):
            with self.subTest(field=field):
                invalid = {**self.updates[0], field: value}
                self.provider.generate_text.return_value = AITextResult(json.dumps({"qualification_updates": [invalid]}), "review")
                draft = _draft()
                self.assertIs(self.recover(draft), draft)

    def test_greetings_short_aliases_completed_flows_and_final_pass_do_not_review(self):
        for body in ("Hello", "A", "Yes", "20"):
            with self.subTest(body=body):
                context = replace(self.context, conversation={"messages": [{"id": "source-current", "direction": "inbound", "body": body}]})
                self.recover(context=context)
        self.recover(state={**self.state, "qualification_completed": True})
        self.recover(context=replace(self.context, stage={"name": "Qualified"}))
        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            self.recover()
        finally:
            _FINAL_LANGUAGE_ONLY.reset(token)
        self.provider.generate_text.assert_not_called()

    def test_review_failure_preserves_valid_original_proposals(self):
        self.provider.generate_text.side_effect = RuntimeError("provider unavailable")
        draft = _draft(self.updates[:1])
        self.assertIs(self.recover(draft), draft)

    def test_obsolete_question_selection_closes_after_complete_recovery(self):
        self.provider.generate_text.side_effect = [
            AITextResult(json.dumps({"qualification_updates": self.updates}), "review"),
            AITextResult('{"message":"Thanks for sharing your setup."}', "reply-repair"),
        ]
        recovered = self.recover(_draft(message="Thanks. " + self.requirements[0]["question"],
            next_requirement_id=self.requirements[0]["id"], reason="QUALIFICATION_NEXT", reason_code="QUALIFICATION_NEXT"))
        self.assertIsNone(recovered.next_requirement_id)
        self.assertEqual(recovered.qualification_updates, self.updates)
        self.assertEqual(recovered.message, "Thanks for sharing your setup.")
        self.assertEqual(recovered.reason_code, "NORMAL_CONVERSATION")
        self.assertEqual(self.provider.generate_text.call_count, 2)

    def test_partial_recovery_closes_old_question_and_keeps_next_goal_pending(self):
        self.provider.generate_text.side_effect = [
            AITextResult(json.dumps({"qualification_updates": self.updates[:1]}), "review"),
            AITextResult('{"message":"Thanks for explaining the slow replies."}', "reply-repair"),
        ]
        recovered = self.recover(_draft(message="Thanks. " + self.requirements[0]["question"],
                                       next_requirement_id=self.requirements[0]["id"]))
        self.assertEqual(recovered.qualification_updates, self.updates[:1])
        self.assertIsNone(recovered.next_requirement_id)
        from apps.ai_engagement.services.qualification_state import project_answer_updates
        projected = project_answer_updates(state=self.state, requirements=self.requirements,
            updates=recovered.qualification_updates, messages=self.context.conversation["messages"])
        self.assertFalse(projected["qualification_completed"])
        self.assertEqual(projected["current_requirement_id"], self.requirements[1]["id"])

    def test_failed_or_still_obsolete_rewrite_keeps_original_valid_draft(self):
        for result in (RuntimeError("temporary reply rewrite failure"),
                       AITextResult(json.dumps({"message": self.requirements[0]["question"]}), "reply-repair")):
            with self.subTest(result_type=type(result).__name__):
                self.provider.generate_text.side_effect = [
                    AITextResult(json.dumps({"qualification_updates": self.updates}), "review"), result,
                ]
                draft = _draft(message="Thanks. " + self.requirements[0]["question"],
                               next_requirement_id=self.requirements[0]["id"])
                self.assertIs(self.recover(draft), draft)

    def test_price_number_decline_never_becomes_a_qualification_answer(self):
        context = replace(self.context, conversation={"messages": [{"id": "source-current", "direction": "inbound",
                                                                   "body": "Is the DIY plan price 2999?"}]})
        self.provider.generate_text.return_value = AITextResult('{"qualification_updates":[]}', "review")
        draft = _draft()
        self.assertIs(self.recover(draft, context=context), draft)

    def test_real_engagement_service_recovers_zero_draft_and_revalidates_rewritten_reply(self):
        self.context.organization["bot_languages"] = "English, Hinglish"
        lead = _SandboxLead(id="lead-1", pk="lead-1", organization_id="org-1", attributes={},
                            stage=SimpleNamespace(name="New leads"), stage_id=None, pipeline_id=None)
        calls = []
        def generate(**kwargs):
            phase = kwargs.get("metadata", {}).get("phase")
            calls.append(phase)
            if phase == "qualification_capture_recovery":
                return AITextResult(json.dumps({"qualification_updates": self.updates}), "review")
            if phase == "qualification_capture_reply_repair":
                return AITextResult('{"message":"Thanks for sharing your setup."}', "reply-repair")
            if phase == "grounding":
                payload = json.loads(kwargs["input_text"])
                self.assertEqual(len(payload["proposed_answer_updates"]), 4)
                self.assertTrue(payload["backend_state"]["qualification_completed"])
                self.assertIsNone(payload["qualification_question_id"])
                self.assertNotIn(self.requirements[0]["question"], payload["reply"])
                return AITextResult('{"approved":true,"reason":"approved"}', "grounding")
            self.assertEqual(phase, "primary")
            return AITextResult(json.dumps({"should_engage": True, "silence_rule": None,
                "message": "Thanks for explaining your setup. " + self.requirements[0]["question"],
                "qualification_updates": [], "crm_actions": [], "file_document_id": None,
                "next_requirement_id": self.requirements[0]["id"], "reason_code": "NORMAL_CONVERSATION"}), "draft")
        cache.clear()
        with patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=generate), \
             patch("apps.ai_engagement.graph.evidence.OpenAIProvider", OpenAIProvider):
            result = EngagementService().engage(organization=self.org, lead=lead, context=self.context)
        self.assertEqual(result.qualification_updates, self.updates)
        self.assertIsNone(result.next_requirement_id)
        self.assertEqual(calls, ["primary", "qualification_capture_recovery", "qualification_capture_reply_repair", "grounding"])


@override_settings(OPENAI_API_KEY="unit-test-unused-key", AI_BRAIN_RECOVERY_ENABLED=False,
                   CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class FirstTurnCaptureRecoverySandboxTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.org = Organization.objects.create(name="Capture recovery company", timezone="Asia/Kolkata")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.new = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.call = Stage.objects.create(pipeline=self.pipeline, name="Call Requested", display_order=80)
        self.brochure = Document.objects.create(organization=self.org, name="Product brochure", file="knowledge/product.pdf",
            processing_status="completed", file_sharing_ready=True,
            share_instruction="Share on welcome or whenever the customer asks for the product brochure.")
        for key, name in (("biggest_problem", "BIGGEST PROBLEM"), ("lead_management_tool", "LEAD MANAGEMENT TOOL"),
                          ("leads_d", "LEADS/D"), ("running_ads", "RUNNING ADS")):
            AttributeDefinition.objects.create(organization=self.org, key=key, name=name)
        info, _ = OrgInfo.objects.get_or_create(organization=self.org)
        info.bot_languages = "English, Hinglish"
        info.about = "We automate customer conversations."
        info.ai_playbook = build_ai_playbook(questions=authored_fixtures._QUESTIONS, rules=(
            "Save supported answers as they arrive; do not wait for all qualification questions.\n"
            "Instagram phone is optional.\n\n## Attribute mapped\n"
            "Mapping 1:\n- Attribute name: BIGGEST PROBLEM\n- Source: Qualification Question 1 or an equivalent explicit customer statement.\n"
            "- Value rule:\n  - Slow replies / slow response / late response → Slow replies.\n"
            "  - Missed follow-ups / forgetting follow-ups → Missed follow-ups.\n\n"
            "Mapping 2:\n- Attribute name: LEAD MANAGEMENT TOOL\n- Source: Qualification Question 2 or an equivalent explicit customer statement.\n"
            "- Value rule:\n  - WhatsApp → WhatsApp.\n  - Excel / Google Sheets → Excel / Sheets.\n\n"
            "Mapping 3:\n- Attribute name: LEADS/D\n- Source: Qualification Question 3 or an equivalent explicit daily-volume statement.\n"
            "- Value rule:\n  - 0–10 leads per day → 0-10.\n  - 11–30 leads per day → 10-30.\n"
            "  - More than 30 leads per day → 30+.\n\n"
            "Mapping 4:\n- Attribute name: RUNNING ADS\n- Source: Qualification Question 4 or an equivalent explicit current-status statement.\n"
            "- Value rule:\n  - Yes / currently running paid ads → Yes.\n  - No / not currently running paid ads → No.\n\n"
            "## Stage shifting\nWhen all required qualification questions are answered, move to Qualified in the current pipeline.\n"
            "When the customer explicitly requests a call or callback, move to Call Requested in the current pipeline.\n"
            "An explicit call request takes priority over qualification completion.\n\n"
            "## Reminder creation logic\nReminder 1:\n"
            "- Create when the customer explicitly requests a callback and provides or confirms a future date and time.\n"
            "- Title: Customer Callback."
        ))
        info.save()
        self.requirements = compile_org_ai_profile_from_context({"ai_playbook": info.ai_playbook})["qualification"]["requirements"]
        self.seen = []
        self.sparse = False

    def provider(self, **kwargs):
        payload = json.loads(kwargs["input_text"])
        phase = kwargs.get("metadata", {}).get("phase")
        self.seen.append((phase, payload))
        if phase == "grounding":
            return AITextResult('{"approved":true,"reason":"approved"}', "recorded-provider")
        if phase == "qualification_capture_recovery":
            source = payload["current_inbound"]
            instagram = "Google Sheets" in source["body"]
            allowed = {item["id"] for item in payload["unanswered_authored_goals"]}
            updates = [item for item in _answers(self.requirements, source["id"], instagram=instagram)
                       if item["requirement_id"] in allowed]
            return AITextResult(json.dumps({"qualification_updates": updates}), "recorded-provider")
        if phase == "file_selection_review":
            return AITextResult(json.dumps({"should_share": True, "document_id": self.brochure.pk,
                                           "reason": "Explicit brochure request satisfies the authored condition."}), "recorded-provider")
        if phase == "qualification_capture_reply_repair":
            return AITextResult('{"message":"Thanks for sharing your setup."}', "recorded-provider")
        if phase == "intent_classification":
            return AITextResult(json.dumps({"primary_intent": "UNKNOWN", "secondary_intents": [], "confidence": .8,
                "entities": [], "facts": [], "direct_question": None, "qualification_candidate": None,
                "requested_action": None, "language": "English", "requires_knowledge": False, "requires_human": False}), "recorded-provider")
        final = (payload.get("response_plan") or {}).get("phase") == "FINAL_COMPOSITION"
        source = next(item for item in reversed(payload["recent_conversation"]["messages"]) if item["direction"] == "inbound")
        callback = "Please call me" in source["body"]
        updates = _answers(self.requirements, source["id"], instagram="Google Sheets" in source["body"])[:1] if self.sparse and not final else []
        question = self.requirements[1 if self.sparse else 0] if not final and not callback else None
        return AITextResult(json.dumps({"should_engage": True, "silence_rule": None,
            "message": ("Your setup is understood; the callback is a preview only." if final and callback
                        else "Thanks for sharing your setup." + (" " + question["question"] if question else "")),
            "file_document_id": None, "qualification_updates": updates, "next_requirement_id": question["id"] if question else None,
            "crm_actions": [{"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.call.pk)}}] if callback and not final else [],
            "reason_code": "NORMAL_CONVERSATION"}), "recorded-provider")

    def test_real_first_turn_service_recovers_zero_or_sparse_draft_on_both_channels(self):
        for channel in ("whatsapp", "instagram"):
            for sparse in (False, True):
                for callback in (False, True):
                    with self.subTest(channel=channel, sparse=sparse, callback=callback):
                        self.sparse, self.seen = sparse, []
                        source = _SOURCE if channel == "whatsapp" else _SOURCE.replace("slow replies", "missed follow-ups").replace("WhatsApp", "Google Sheets")
                        if callback:
                            source += " Please call me tomorrow at 3 PM India time and share the product brochure."
                        session = f"capture-{channel}-{sparse}-{callback}"
                        with patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=self.provider), \
                             patch("apps.ai_engagement.graph.evidence.OpenAIProvider", OpenAIProvider), \
                             patch("apps.ai_engagement.services.embeddings.EmbeddingService._get_client", side_effect=EmbeddingError("no live embeddings")):
                            result = PlaygroundService().run(organization=self.org, session_id=session,
                                message=source, channel=channel, lead_source=channel, stage_id=str(self.new.pk))
                        self.assertEqual(result.stage["id"], str(self.call.pk if callback else self.qualified.pk))
                        attributes = next(event["updates"] for event in result.events if event["type"] == "attribute_updates")
                        self.assertEqual({item["key"]: item["value"] for item in attributes}, {
                            "biggest_problem": "Missed follow-ups" if channel == "instagram" else "Slow replies",
                            "lead_management_tool": "Excel / Sheets" if channel == "instagram" else "WhatsApp",
                            "leads_d": "10-30", "running_ads": "No",
                        })
                        saved = PlaygroundService()._load_session_payload(organization=self.org, session_id=session)
                        self.assertTrue(saved["attributes"][QUALIFICATION_STATE_KEY]["qualification_completed"])
                        final = [payload for _phase, payload in self.seen if (payload.get("response_plan") or {}).get("phase") == "FINAL_COMPOSITION"]
                        self.assertTrue(final)
                        self.assertIsNone(final[-1]["response_plan"]["next_question"])
                        self.assertEqual(sum(phase == "qualification_capture_recovery" for phase, _payload in self.seen), 1)
                        self.assertTrue(any(phase == "grounding" for phase, _payload in self.seen))
                        if callback:
                            self.assertEqual([item["id"] for item in result.files], [self.brochure.pk])
                            reminder = next(event for event in result.events if event["type"] == "reminder")
                            self.assertEqual(reminder["title"], "Customer Callback")
                            self.assertIn("T15:00:00+05:30", reminder["due_at"])
                        self.assertEqual(Lead.objects.filter(organization=self.org).count(), 0)
                        self.assertEqual(LeadReminder.objects.filter(lead__organization=self.org).count(), 0)
                        self.assertEqual(AIActionReceipt.objects.filter(organization=self.org).count(), 0)
