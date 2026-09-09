import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.qualification_state import (
    next_requirement, project_answer_updates, state_for_lead,
)
from apps.ai_engagement.tests.test_precise_orchestration import PreciseEngagementTests


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SemanticAnswerTests(SimpleTestCase):
    def test_free_form_answer_advances_question_in_same_call(self):
        context = PreciseEngagementTests()._context("I live in Delhi")
        context.organization["qualification_requirements"] = "Which city?\nWhat is your budget?"
        context.conversation["messages"][0]["id"] = "inbound-1"
        output = {
            "should_engage": True, "message": "What budget do you have in mind?",
            "crm_actions": [], "file_document_id": None,
            "qualification_updates": [{"requirement_id": "which_city", "value": "Delhi",
                                       "source_message_id": "inbound-1", "evidence": "Delhi"}],
            "next_requirement_id": "what_is_your_budget", "reason_code": "QUALIFICATION_NEXT",
        }
        provider = Mock()
        provider.generate_text.return_value = AITextResult(json.dumps(output), "test")
        with patch("apps.ai_engagement.services.engagement.EngagementGenerationLock") as lock:
            lock.return_value.acquire.return_value = True
            result = EngagementService(provider=provider).engage(
                organization=SimpleNamespace(id="org-1"), lead=SimpleNamespace(id="lead-1"),
                context=context,
            )
        self.assertEqual(result.next_requirement_id, "what_is_your_budget")
        self.assertEqual(result.qualification_updates[0]["value"], "Delhi")
        self.assertEqual(provider.generate_text.call_count, 1)

    def test_invented_evidence_and_unknown_requirements_are_rejected(self):
        requirements = [{"id": "city", "priority": 1}]
        state = state_for_lead(SimpleNamespace(), requirements=requirements)
        for requirement, evidence in [("city", "Mumbai"), ("other", "Delhi")]:
            with self.assertRaises(ValueError):
                project_answer_updates(
                    state=state, requirements=requirements,
                    updates=[{"requirement_id": requirement, "value": "Mumbai",
                              "source_message_id": "1", "evidence": evidence}],
                    messages=[{"id": "1", "direction": "inbound", "body": "Delhi"}],
                )
        self.assertEqual(next_requirement(requirements, state["requirement_states"])["id"], "city")

    def test_retry_reuses_generated_decision_without_second_provider_call(self):
        cache.clear()
        context = PreciseEngagementTests()._context("Hello")
        context.conversation["messages"][0]["id"] = "retry-1"
        provider = Mock()
        provider.generate_text.return_value = AITextResult(json.dumps({
            "should_engage": True, "message": "How can I help?", "file_document_id": None,
            "crm_actions": [], "next_requirement_id": None, "reason_code": "NORMAL_CONVERSATION",
        }), "test")
        with patch("apps.ai_engagement.services.engagement.EngagementGenerationLock") as lock:
            lock.return_value.acquire.side_effect = [True, False]
            service = EngagementService(provider=provider)
            args = dict(organization=SimpleNamespace(id="org-1"),
                        lead=SimpleNamespace(id="lead-1"), context=context)
            first = service.engage(**args)
            second = service.engage(**args)
        self.assertEqual(first, second)
        self.assertEqual(provider.generate_text.call_count, 1)


class ContextAndEnrichmentTests(TestCase):
    def setUp(self):
        from apps.crm.models import Pipeline, Stage, Lead, AttributeDefinition
        from apps.organizations.models import Organization
        cache.clear()
        self.org = Organization.objects.create(name="Reliability")
        pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = Stage.objects.create(pipeline=pipeline, name="New Lead", display_order=0)
        self.lead = Lead.objects.create(organization=self.org, pipeline=pipeline,
                                       stage=self.stage, name="Test", phone="+919876501234")
        AttributeDefinition.objects.create(organization=self.org, name="City", key="city")

    def test_context_exposes_empty_attribute_definitions_and_valid_stages(self):
        from apps.ai_engagement.services.context import AIContextBuilder
        data = AIContextBuilder()._build_pipeline_context(lead=self.lead)
        self.assertEqual(data["attribute_definitions"][0]["key"], "city")
        self.assertIn(str(self.stage.id), [stage["id"] for stage in data["available_stages"]])

    def test_recording_next_question_preserves_just_written_crm_attributes(self):
        from apps.ai_engagement.models import OrgInfo
        from apps.ai_engagement.background_signals import remember_ai_qualification_question
        from apps.crm.models import Lead
        OrgInfo.objects.update_or_create(organization=self.org,
                                        defaults={"qualification_requirements": "Which city?"})
        # The outbound message still holds the pre-CRM-action Lead instance.
        Lead.objects.filter(pk=self.lead.pk).update(attributes={"city": "Delhi"})
        instance = SimpleNamespace(
            lead=self.lead, lead_id=self.lead.pk, organization=self.org,
            direction="outbound", raw_payload={"shvya_ai": {
                "reason": "ANSWER_ORG_QUESTION", "next_requirement_id": "which_city",
            }},
        )
        remember_ai_qualification_question(None, instance, created=False)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["city"], "Delhi")
        self.assertEqual(self.lead.attributes["_shvya_ai_qualification"]["last_asked_requirement_id"], "which_city")

    @patch("apps.ai_engagement.tasks.flush_background_enrichment.apply_async")
    def test_short_conversation_has_one_eventual_refresh(self, flush):
        from apps.ai_engagement.services.background_enrichment import queue_background_enrichment
        with patch("apps.ai_engagement.services.background_enrichment.enrichment_due", return_value=False):
            queue_background_enrichment(lead_id=self.lead.id)
            queue_background_enrichment(lead_id=self.lead.id)
        flush.assert_called_once_with(args=[str(self.lead.id)], countdown=20)

    @patch("apps.ai_engagement.tasks.generate_lead_qualification.apply_async")
    @patch("apps.ai_engagement.tasks.generate_internal_conversation_summary.delay")
    def test_flush_refreshes_both_summary_and_qualification_note(self, summary, qualification):
        from apps.ai_engagement.models import OrgInfo
        from apps.ai_engagement.tasks import flush_background_enrichment
        OrgInfo.objects.update_or_create(organization=self.org,
                                        defaults={"qualification_requirements": "Which city?"})
        flush_background_enrichment(str(self.lead.id))
        summary.assert_called_once_with(str(self.lead.id))
        qualification.assert_called_once_with(args=[str(self.lead.id)], countdown=10)
