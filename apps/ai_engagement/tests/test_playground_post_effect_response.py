"""Sandbox post-effect regressions: mocked providers, never customer sends."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai_engagement.models import Document, OrgInfo
from apps.ai_engagement.services.ai_provider import AIProviderTransientError, AITextResult, OpenAIProvider
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.playground import PlaygroundError, PlaygroundService, _SandboxLead
from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY
from apps.channels.models import WhatsAppMessage
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, LeadNote, LeadReminder, Pipeline, Stage
from apps.organizations.models import Organization


def decision(message="A supported answer.", **kwargs):
    data = dict(should_engage=True, message=message, file_document_id=None,
                crm_actions=[], qualification_updates=[], reason="NORMAL_CONVERSATION",
                reason_code="NORMAL_CONVERSATION", model="test")
    data.update(kwargs)
    return EngagementDecision(**data)


class SandboxFinalLanguageBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.org = SimpleNamespace(id="organization-test")
        self.visitor = _SandboxLead(
            id="playground:test", organization_id=self.org.id,
            stage=SimpleNamespace(name="Current"), stage_id="current",
            pipeline=SimpleNamespace(name="Sales"), pipeline_id="sales",
            attributes={"industry": "Retail"}, preview_reminder={"title": "Call", "due_at": "2030-01-01T10:00:00+05:30"},
            shared_document_ids=[5],
        )
        self.context = AIContext(
            organization={"id": self.org.id}, lead={"id": self.visitor.id, "operational_state": {
                "execution_mode": "sandbox_preview", "reminder": deepcopy(self.visitor.preview_reminder),
            }}, pipeline={}, stage={}, contacts=[], attributes=[], conversation={"messages": []},
            conversation_summary=None, qualification_notes=[], knowledge=[],
        )
        self.builder = SimpleNamespace(
            pipeline=self.visitor.pipeline, stage=self.visitor.stage,
            conversation=[{"id": "this-turn", "direction": "inbound", "body": "Call tomorrow"}],
            last_knowledge=[{"content": "Approved fact", "document_id": "5"}],
            build=lambda **kwargs: self.context,
        )
        self.provider = object()
        self.old_builder = object()
        self.engine = SimpleNamespace(provider=self.provider, context_builder=self.old_builder, engage=self.generate)
        self.service = PlaygroundService()
        self.final = decision()
        self.seen_context = None
        self.events = [{"type": "reminder", "status": "preview"}]

    def generate(self, **kwargs):
        self.assertTrue(_FINAL_LANGUAGE_ONLY.get())
        self.seen_context = kwargs["context"]
        self.visitor.attributes["industry"] = "must be discarded"
        self.visitor.preview_reminder["title"] = "must be discarded"
        self.visitor.shared_document_ids.append(99)
        self.visitor.stage_id = "must be discarded"
        self.visitor.transient_field = "must be discarded"
        return self.final

    def compose(self, files=None, draft=None):
        return self.service._compose_after_preview(
            organization=self.org, visitor=self.visitor, message="Call tomorrow",
            service=self.engine, context_builder=self.builder, decision=draft or decision(),
            files=files or [], events=self.events,
        )

    def test_reminder_and_source_reach_final_context_without_fake_stage_action(self):
        self.compose()
        operational = self.seen_context.lead["operational_state"]
        self.assertEqual(operational["reminder"]["title"], "Call")
        self.assertEqual(operational["resolved_actions"]["source_message_id"], "this-turn")
        self.assertEqual(operational["resolved_actions"]["action_types"], ["create_reminder"])

    def test_language_pass_restores_visitor_provider_builder_and_flag(self):
        self.compose()
        self.assertEqual(self.visitor.attributes["industry"], "Retail")
        self.assertEqual(self.visitor.preview_reminder["title"], "Call")
        self.assertEqual(self.visitor.shared_document_ids, [5])
        self.assertEqual(self.visitor.stage_id, "current")
        self.assertFalse(hasattr(self.visitor, "transient_field"))
        self.assertIs(self.engine.provider, self.provider)
        self.assertIs(self.engine.context_builder, self.old_builder)
        self.assertFalse(_FINAL_LANGUAGE_ONLY.get())

    def test_final_proposals_cannot_change_resolved_effects(self):
        self.final = decision(crm_actions=[{"type": "add_note", "note": "No"}],
                              qualification_updates=[{"value": "No"}], file_document_id=99)
        final = self.compose()
        self.assertEqual(final.crm_actions, [])
        self.assertEqual(final.qualification_updates, [])
        self.assertIsNone(final.file_document_id)

    def test_candidate_rebuild_sees_recovered_evidence_and_selection_stays_fixed(self):
        with patch("apps.ai_engagement.services.file_sharing.FileSharingService.build_file_candidates",
                   return_value=[{"document_id": 5}, {"document_id": 99}]) as candidates:
            self.final = decision(file_document_id=99)
            final = self.compose(files=[{"id": 5, "name": "Approved guide"}], draft=decision(file_document_id=5))
        self.assertEqual(candidates.call_args.kwargs["context"].knowledge, self.builder.last_knowledge)
        self.assertEqual(self.seen_context.organization["_file_candidates"], [{"document_id": 5}])
        self.assertEqual(final.file_document_id, 5)
        self.assertEqual(self.seen_context.lead["operational_state"]["resolved_actions"]["file_share"]["status"], "preview")

    def test_failed_final_pass_restores_state_before_fallback_and_strips_its_actions(self):
        def broken(**kwargs):
            self.generate(**kwargs)
            raise RuntimeError("simulated provider failure")
        def fallback(**kwargs):
            self.assertEqual(self.visitor.attributes["industry"], "Retail")
            self.visitor.preview_reminder["title"] = "also discarded"
            return decision(crm_actions=[{"type": "add_note", "note": "No"}], file_document_id=99)
        self.engine.engage = broken
        with patch.object(self.service, "_fallback_decision", side_effect=fallback):
            final = self.compose()
        self.assertEqual(final.crm_actions, [])
        self.assertIsNone(final.file_document_id)
        self.assertEqual(self.visitor.preview_reminder["title"], "Call")
        self.assertFalse(_FINAL_LANGUAGE_ONLY.get())
        self.assertIs(self.engine.context_builder, self.old_builder)

    def test_transient_final_pass_preserves_validated_draft(self):
        def rate_limited(**kwargs):
            self.generate(**kwargs)
            raise AIProviderTransientError("try again in 1s", retry_after=1)

        self.engine.engage = rate_limited
        with patch.object(self.service, "_fallback_decision", side_effect=AssertionError("fallback must not replace a valid draft")):
            final = self.compose()
        self.assertEqual(final.message, "A supported answer.")
        self.assertEqual(final.crm_actions, [])
        self.assertEqual(final.qualification_updates, [])
        self.assertIsNone(final.file_document_id)
        self.assertEqual(self.visitor.attributes["industry"], "Retail")

    def test_nested_language_flag_restores_prior_value(self):
        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            self.compose()
            self.assertTrue(_FINAL_LANGUAGE_ONLY.get())
        finally:
            _FINAL_LANGUAGE_ONLY.reset(token)

    def test_persisted_lead_is_rejected_before_generation(self):
        self.visitor = SimpleNamespace(**vars(self.visitor))
        with self.assertRaises(PlaygroundError):
            self.compose()
        self.assertIsNone(self.seen_context)
        self.assertIs(self.engine.context_builder, self.old_builder)
        self.assertFalse(_FINAL_LANGUAGE_ONLY.get())

    def test_cross_organization_preview_is_rejected_before_generation(self):
        self.org = SimpleNamespace(id="other-organization")
        with self.assertRaises(PlaygroundError):
            self.compose()
        self.assertIsNone(self.seen_context)
        self.assertFalse(_FINAL_LANGUAGE_ONLY.get())


@override_settings(OPENAI_API_KEY="unit-test-unused-key", AI_BRAIN_RECOVERY_ENABLED=False)
class SandboxPostEffectGraphTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.org = Organization.objects.create(name="Post-effect Sandbox company")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage, _ = Stage.objects.get_or_create(pipeline=self.pipeline, name="Qualified", defaults={"display_order": 1})
        AttributeDefinition.objects.get_or_create(organization=self.org, key="industry", defaults={"name": "Industry"})
        self.info, _ = OrgInfo.objects.get_or_create(organization=self.org)
        self.info.about = "We automate customer conversations."
        self.info.bot_languages = "English"
        self.info.ai_playbook = (
            "## Attribute mapping logic\n"
            "Industry: Save the industry explicitly stated by the customer.\n"
            "## Reminder creation logic\n"
            "Create a reminder when the customer requests a follow-up at a specific time."
        )
        self.info.save()
        self.actions = []
        self.file_id = None
        self.messages = []
        self.final_reply = "The preview uses your updated details."
        self.fail_final = False
        provider = patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=self.provider)
        provider.start()
        self.addCleanup(provider.stop)
        grounding = patch("apps.ai_engagement.graph.evidence.OpenAIProvider", new=OpenAIProvider)
        grounding.start()
        self.addCleanup(grounding.stop)
        embeddings = patch("apps.ai_engagement.services.embeddings.EmbeddingService._get_client", side_effect=EmbeddingError("no live embeddings"))
        embeddings.start()
        self.addCleanup(embeddings.stop)

    def provider(self, **kwargs):
        payload = json.loads(kwargs["input_text"])
        phase = kwargs.get("metadata", {}).get("phase")
        if phase == "grounding":
            return AITextResult('{"approved":true,"reason":"approved"}', "test-transport")
        if phase == "intent_classification":
            return AITextResult(json.dumps({
                "primary_intent": "UNKNOWN", "secondary_intents": [], "confidence": 0.8,
                "entities": [], "facts": [], "direct_question": None, "qualification_candidate": None,
                "requested_action": None, "language": "English", "requires_knowledge": False, "requires_human": False,
            }), "test-transport")
        self.messages.append(payload)
        final = (payload.get("response_plan") or {}).get("phase") == "FINAL_COMPOSITION"
        if final and self.fail_final:
            raise RuntimeError("simulated final provider failure")
        return AITextResult(json.dumps({
            "should_engage": True, "silence_rule": None,
            "message": self.final_reply if final else "Draft response before preview effects.",
            "file_document_id": self.file_id,
            "crm_actions": [] if final else self.actions,
            "qualification_updates": [], "next_requirement_id": None,
            "reason_code": "NORMAL_CONVERSATION",
        }), "test-transport")

    def counts(self):
        return {model.__name__: model.objects.count()
                for model in (Lead, LeadActivity, LeadNote, LeadReminder, WhatsAppMessage)}

    def run_turn(self, session, message, channel="sandbox", stage=True):
        return PlaygroundService().run(
            organization=self.org, session_id=session, message=message,
            stage_id=str(self.stage.pk) if stage else None, channel=channel,
        )

    def final_payload(self):
        finals = [item for item in self.messages if (item.get("response_plan") or {}).get("phase") == "FINAL_COMPOSITION"]
        self.assertEqual(len(finals), 1, self.messages)
        return finals[0]

    def test_attribute_only_effect_is_composed_from_resulting_state(self):
        self.actions = [{"type": "attribute_updates", "updates": [{"key": "industry", "value": "Retail"}]}]
        before = self.counts()
        result = self.run_turn("attribute", "My industry is Retail.")
        self.assertTrue(any(event["type"] == "attribute_updates" for event in result.events), result)
        final = self.final_payload()
        operational = final["lead"]["operational_state"]
        self.assertIn("attribute_updates", operational["resolved_actions"]["action_types"])
        self.assertNotIn("pipeline_transition", operational["resolved_actions"]["action_types"])
        self.assertIn(self.final_reply, result.response)
        self.assertEqual(self.counts(), before)

    def test_reminder_only_effect_is_composed_for_each_preview_channel(self):
        self.actions = [{"type": "create_reminder", "title": "Follow up", "description": "Customer requested a call",
                         "due_at": "2030-01-01T10:00:00+05:30"}]
        before = self.counts()
        for channel in ("sandbox", "whatsapp", "instagram"):
            with self.subTest(channel=channel):
                self.messages = []
                result = self.run_turn("reminder-" + channel, "Please call me on 1 January 2030 at 10 AM IST.", channel)
                self.assertTrue(any(event["type"] == "reminder" for event in result.events), result)
                final = self.final_payload()
                operational = final["lead"]["operational_state"]
                self.assertEqual(operational["reminder"]["title"], "Follow up")
                self.assertIn("create_reminder", operational["resolved_actions"]["action_types"])
                self.assertNotIn("pipeline_transition", operational["resolved_actions"]["action_types"])
                self.assertEqual(final["recent_conversation"]["channel"], channel)
                self.assertIn(self.final_reply, result.response)
        self.assertEqual(self.counts(), before)

    def test_guided_file_without_stage_change_gets_preview_only_final_state(self):
        document = Document.objects.create(
            organization=self.org, name="Guide", file="knowledge/guide.pdf", is_active=True,
            processing_status="completed", share_instruction="Send when the customer asks for the guide.",
        )
        self.file_id = document.pk
        before = self.counts()
        result = self.run_turn("file", "Please send the guide.")
        self.assertEqual([item["id"] for item in result.files], [document.pk])
        final = self.final_payload()
        resolved = final["lead"]["operational_state"]["resolved_actions"]
        self.assertEqual(resolved["action_types"], ["file_share"])
        self.assertEqual(resolved["file_share"]["status"], "preview")
        self.assertEqual(self.counts(), before)

    def test_file_only_composition_works_without_an_active_pipeline(self):
        Pipeline.objects.filter(organization=self.org).update(is_active=False)
        document = Document.objects.create(
            organization=self.org, name="Welcome guide", file="knowledge/welcome.pdf", is_active=True,
            processing_status="completed", share_instruction="Send when the customer asks for the welcome guide.",
        )
        self.file_id = document.pk
        before = self.counts()
        result = self.run_turn("no-pipeline", "Please send the welcome guide.", stage=False)
        self.assertEqual([item["id"] for item in result.files], [document.pk])
        self.assertEqual(self.final_payload()["lead"]["operational_state"]["resolved_actions"]["action_types"], ["file_share"])
        self.assertEqual(self.counts(), before)

    def test_plain_turn_does_not_add_a_final_model_pass(self):
        self.run_turn("plain", "Thank you")
        self.assertFalse(any((item.get("response_plan") or {}).get("phase") == "FINAL_COMPOSITION" for item in self.messages))

    def test_prior_reminder_is_context_not_a_new_completed_action(self):
        PlaygroundService()._save_history(
            organization=self.org, session_id="old-reminder", history=[], stage_id=str(self.stage.pk),
            reminder={"title": "Old reminder", "description": "Earlier turn", "due_at": "2030-01-01T10:00:00+05:30"},
        )
        self.actions = [{"type": "attribute_updates", "updates": [{"key": "industry", "value": "Retail"}]}]
        self.run_turn("old-reminder", "My industry is Retail.")
        operational = self.final_payload()["lead"]["operational_state"]
        self.assertEqual(operational["reminder"]["title"], "Old reminder")
        self.assertNotIn("create_reminder", operational["resolved_actions"]["action_types"])

    def test_attribute_without_authored_mapping_remains_blocked(self):
        self.info.ai_playbook = "## Rules\nAnswer customer questions."
        self.info.save()
        self.actions = [{"type": "attribute_updates", "updates": [{"key": "industry", "value": "Retail"}]}]
        before = self.counts()
        result = self.run_turn("unmapped", "My industry is Retail.")
        self.assertEqual(result.events, [])
        saved = PlaygroundService()._load_session_payload(organization=self.org, session_id="unmapped")
        self.assertNotIn("industry", saved["attributes"])
        self.assertEqual(self.counts(), before)
