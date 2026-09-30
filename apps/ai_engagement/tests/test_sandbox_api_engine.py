"""Exercise the Sandbox HTTP contract through the real engagement graph."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.ai_engagement.models import Document, OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult, OpenAIProvider
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.playground import PlaygroundService
from apps.ai_engagement.services.qualification_state import QUALIFICATION_STATE_KEY
from apps.ai_engagement.views.playground import PlaygroundAPIView
from apps.channels.models import WhatsAppMessage
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, LeadNote, LeadReminder, Pipeline, Stage
from apps.organizations.models import Organization


@override_settings(OPENAI_API_KEY="unit-test-unused-key")
class SandboxAPIEngineTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Sandbox engine company")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.new, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, name="New Lead", defaults={"display_order": 0},
        )
        self.qualified, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, name="Qualified", defaults={"display_order": 1},
        )
        self.demo = Stage.objects.create(
            pipeline=self.pipeline,
            name="Demo Requested",
            description="Lead requests a demo.",
            display_order=90,
        )
        self.info, _ = OrgInfo.objects.get_or_create(organization=self.org)
        self.info.about = "We automate customer conversations."
        self.info.bot_languages = "English"
        self.info.ai_playbook = (
            "## Stage shifting logic\n"
            "Move to Demo Requested when the lead requests a demo."
        )
        self.info.save()
        self.messages = []
        self.grounding_messages = []
        self.final_reply = None
        self.final_actions = None
        self.final_file_id = None
        self.reply = "Thanks for your interest. What would you like to see?"
        self.actions = []
        self.file_id = None
        self.next_requirement_id = None
        self.reason_code = "NORMAL_CONVERSATION"
        provider = patch(
            "apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text",
            side_effect=self._provider,
        )
        provider.start()
        self.addCleanup(provider.stop)
        # Override the suite's blanket verifier fixture: these API regressions
        # exercise the real grounding gate through the same transport boundary.
        grounding = patch("apps.ai_engagement.graph.evidence.OpenAIProvider", new=OpenAIProvider)
        grounding.start()
        self.addCleanup(grounding.stop)
        embeddings = patch(
            "apps.ai_engagement.services.embeddings.EmbeddingService._get_client",
            side_effect=EmbeddingError("test transport unavailable"),
        )
        embeddings.start()
        self.addCleanup(embeddings.stop)
        self.factory = APIRequestFactory()

    def _provider(self, **kwargs):
        payload = json.loads(kwargs["input_text"])
        phase = kwargs.get("metadata", {}).get("phase")
        if phase == "grounding":
            self.grounding_messages.append(payload)
            return AITextResult('{"approved":true,"reason":"supported by fixture"}', "test-transport")
        if phase == "intent_classification":
            return AITextResult(json.dumps({
                "primary_intent": "UNKNOWN", "secondary_intents": [],
                "confidence": 0.8, "entities": [], "facts": [],
                "direct_question": None, "qualification_candidate": None,
                "requested_action": None, "language": "English",
                "requires_knowledge": False, "requires_human": False,
            }), "test-transport")
        self.messages.append(payload)
        final = (payload.get("response_plan") or {}).get("phase") == "FINAL_COMPOSITION"
        return AITextResult(json.dumps({
            "should_engage": True, "silence_rule": None,
            "message": self.final_reply if final and self.final_reply else self.reply,
            "file_document_id": self.final_file_id if final and self.final_file_id else self.file_id,
            "crm_actions": self.final_actions if final and self.final_actions is not None else self.actions,
            "qualification_updates": [], "next_requirement_id": self.next_requirement_id,
            "reason_code": self.reason_code,
        }), "test-transport")

    def _request(self, *, session_id, message=None, stage=None, organization=None):
        data = {"session_id": session_id}
        if message is None:
            request = self.factory.delete("/api/v1/ai-engagement/playground/", data, format="json")
        else:
            data["message"] = message
            if stage is not None:
                data["stage_id"] = str(stage.pk)
            request = self.factory.post("/api/v1/ai-engagement/playground/", data, format="json")
        force_authenticate(request, user=SimpleNamespace(
            is_authenticated=True, organization=organization or self.org,
        ))
        response = PlaygroundAPIView.as_view()(request)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def _business_counts(self):
        return {
            model.__name__: model.objects.count()
            for model in (Lead, LeadActivity, LeadNote, LeadReminder, WhatsAppMessage)
        }

    def test_real_api_graph_shares_guided_file_without_pipeline(self):
        Pipeline.objects.filter(organization=self.org).update(is_active=False)
        document = Document.objects.create(
            organization=self.org, name="Welcome guide", file="knowledge/welcome.pdf",
            processing_status=Document.ProcessingStatus.COMPLETED,
            share_instruction="Send this welcome guide when the customer says hello.",
        )
        self.file_id = document.pk
        self.reply = "Hello! Here is the welcome guide."
        before = self._business_counts()

        result = self._request(session_id="no-pipeline", message="Hello")

        self.assertEqual([item["id"] for item in result["files"]], [document.pk])
        self.assertEqual(result["files"][0]["url"], f"/api/v1/ai-engagement/playground/files/{document.pk}/")
        self.assertTrue(any(
            any(item["document_id"] == document.pk for item in payload.get("file_candidates", []))
            for payload in self.messages
        ))
        self.assertEqual(self._business_counts(), before)

    def test_real_api_graph_previews_stage_and_remembers_it_after_next_turn(self):
        self.actions = [{"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.demo.pk)}}]
        before = self._business_counts()

        first = self._request(session_id="stage", message="I want a demo", stage=self.qualified)
        self.assertEqual(first["stage"]["id"], str(self.demo.pk))
        self.assertEqual(first["events"][0]["stage"], self.demo.name)
        self.actions = []
        self.reply = "You're welcome!"
        second = self._request(session_id="stage", message="Thank you")

        self.assertEqual(second["stage"]["id"], str(self.demo.pk))
        self.assertEqual(self.messages[-1]["stage"]["id"], str(self.demo.pk))
        self.assertEqual(self._business_counts(), before)

    def test_post_transition_reply_uses_final_stage_and_cannot_change_resolved_effects(self):
        document = Document.objects.create(
            organization=self.org, name="Demo guide", file="knowledge/demo.pdf",
            processing_status=Document.ProcessingStatus.COMPLETED,
            share_instruction="Send when the customer requests a demo.",
        )
        different_document = Document.objects.create(
            organization=self.org, name="Other guide", file="knowledge/other.pdf",
            processing_status=Document.ProcessingStatus.COMPLETED,
            share_instruction="Send when the customer requests another guide.",
        )
        self.actions = [{"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.demo.pk)}}]
        self.file_id = document.pk
        self.reply = "What would you like to see?"
        self.final_reply = "Here is the demo guide."
        self.final_actions = [
            {"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.new.pk)}},
            {"type": "add_note", "note": "Second-pass proposal must never be applied."},
        ]
        self.final_file_id = different_document.pk
        before = self._business_counts()

        result = self._request(session_id="post-stage", message="I want a demo", stage=self.qualified)

        self.assertEqual(len(self.messages), 2)
        draft, final = self.messages
        self.assertEqual(draft["stage"]["id"], str(self.qualified.pk))
        self.assertEqual(final["stage"]["id"], str(self.demo.pk))
        self.assertEqual(final["response_plan"]["phase"], "FINAL_COMPOSITION")
        self.assertEqual([item["document_id"] for item in final["file_candidates"]], [document.pk])
        self.assertEqual(final["lead"]["operational_state"]["resolved_actions"]["file_share"]["document_id"], document.pk)
        self.assertEqual(self.grounding_messages[-1]["operational_state"]["resolved_actions"]["stage"]["id"], str(self.demo.pk))
        self.assertEqual(result["stage"]["id"], str(self.demo.pk))
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual([item["id"] for item in result["files"]], [document.pk])
        self.assertIn(self.final_reply, result["response"])
        self.assertNotIn(self.reply, result["response"])
        saved = PlaygroundService()._load_session_payload(organization=self.org, session_id="post-stage")
        self.assertEqual(saved["sent_files"], [document.pk])
        self.assertEqual(saved["history"][-1]["content"], result["response"])
        self.assertEqual(self._business_counts(), before)

    def test_real_api_graph_rejects_foreign_file_and_unsupported_stage(self):
        other = Organization.objects.create(name="Foreign organization")
        foreign_file = Document.objects.create(
            organization=other, name="Private", file="knowledge/private.pdf",
            processing_status=Document.ProcessingStatus.COMPLETED,
            share_instruction="Send when requested.",
        )
        self.file_id = foreign_file.pk
        self.actions = [{"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.demo.pk)}}]
        self.reply = "You're welcome!"

        result = self._request(session_id="guarded", message="Thank you", stage=self.qualified)

        self.assertEqual(result["files"], [])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["stage"]["id"], str(self.qualified.pk))

    def test_real_api_context_applies_live_attribute_confidentiality_rules(self):
        for key in ("industry", "password", "booked_at"):
            AttributeDefinition.objects.get_or_create(
                organization=self.org, key=key, defaults={"name": key},
            )
        PlaygroundService()._save_history(
            organization=self.org, session_id="private-attributes", history=[],
            stage_id=str(self.qualified.pk),
            attributes={
                "industry": "Retail", "password": "private-fixture",
                "details": {"city": "Pune", "client_secret": "nested-fixture"},
                "_internal": "private-state-fixture",
            },
        )
        self.reply = "You're welcome!"

        self._request(session_id="private-attributes", message="Thank you")

        self.assertTrue(self.messages)
        for payload in self.messages:
            attributes = {item["name"]: item["value"] for item in payload.get("attributes", [])}
            self.assertEqual(attributes.get("industry"), "Retail")
            self.assertEqual(attributes.get("details"), {"city": "Pune"})
            self.assertNotIn("password", attributes)
            self.assertNotIn("_internal", attributes)
            definition_keys = {
                item["key"] for item in payload.get("pipeline", {}).get("attribute_definitions", [])
            }
            self.assertNotIn("password", definition_keys)
            self.assertNotIn("booked_at", definition_keys)

    def test_qualification_completion_persists_live_lifecycle_in_session_only(self):
        from apps.ai_engagement.services.organization_profile import compile_qualification_requirements

        questions = "What is your industry?"
        self.info.ai_playbook = (
            f"## Qualification Questions\n{questions}\n"
            "## Qualification Criteria\nAll qualification questions must be answered.\n"
            "## Stage shifting logic\nWhen all qualification questions are answered, move to Qualified."
        )
        self.info.save()
        requirement = compile_qualification_requirements(questions)["requirements"][0]
        self.reply = requirement["question"]
        self.next_requirement_id = requirement["id"]
        self.reason_code = "QUALIFICATION_NEXT"
        before = self._business_counts()
        self._request(session_id="complete", message="Hello", stage=self.new)
        self.reply = "Thanks for sharing."
        self.next_requirement_id = None
        self.reason_code = "NORMAL_CONVERSATION"

        result = self._request(session_id="complete", message="Retail")

        self.assertEqual(result["stage"]["id"], str(self.qualified.pk))
        saved = PlaygroundService()._load_session_payload(organization=self.org, session_id="complete")
        state = saved["attributes"][QUALIFICATION_STATE_KEY]
        self.assertEqual(state["qualification_status"], "completed")
        self.assertEqual(state["qualification_result"], "qualified")
        self.assertEqual(state["engagement_mode"], "conversation")
        self.assertIsNone(state["next_requirement_id"])
        self.assertEqual(self._business_counts(), before)
        self._request(session_id="complete")
        self.assertEqual(PlaygroundService()._load_session_payload(
            organization=self.org, session_id="complete"), {})
