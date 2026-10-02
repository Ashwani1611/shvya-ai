"""Live Instagram turns share the canonical scoped evidence contract."""
import json
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.services import conversation_policy_runtime, intent_runtime
from apps.ai_engagement.services import phase5_6_runtime as runtime
from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.intent_types import ClassificationPath, Intent, IntentDecision
from apps.ai_engagement.services.tenant_guard import TenantScopeError
from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramMessage
from apps.crm.models import Lead
from apps.organizations.models import Organization
from services.channels.instagram_ai import InstagramAIContextBuilder


class InstagramEvidenceContextTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Instagram evidence", settings={"pricing": {"pro": "₹999/month"}},
        )
        pipeline = self.org.pipelines.first()
        self.lead = Lead.objects.create(
            organization=self.org, pipeline=pipeline, stage=pipeline.stages.first(),
            name="Instagram customer", phone="", lead_source="instagram",
        )
        self.account = InstagramAccount.objects.create(
            organization=self.org, ig_user_id="evidence-business",
            username="evidence_test", access_token="test-token",
            status=InstagramAccount.Status.CONNECTED,
        )
        self.conversation = InstagramConversation.objects.create(
            organization=self.org, account=self.account, lead=self.lead,
            participant_id="evidence-customer", participant_username="customer",
        )
        self.source = InstagramMessage.objects.create(
            organization=self.org, account=self.account, conversation=self.conversation,
            direction=InstagramMessage.Direction.INBOUND, status=InstagramMessage.Status.RECEIVED,
            external_id="evidence-inbound", sender_id="evidence-customer",
            recipient_id="evidence-business", body="What is your price?",
        )
        self.intent = IntentDecision(
            primary_intent=Intent.PRICING_QUESTION, confidence=0.99,
            direct_question=self.source.body,
            classification_path=ClassificationPath.DETERMINISTIC, requires_knowledge=True,
        )

    def test_instagram_input_contains_verified_evidence_and_exact_source_identity(self):
        before = deepcopy(self.lead.attributes)
        service = EngagementService(
            context_builder=InstagramAIContextBuilder(conversation_id=self.conversation.pk),
        )
        with patch(
            "apps.ai_engagement.services.intent_engine.IntentEngine.classify", return_value=self.intent,
        ) as classify:
            with runtime.source_evidence_context(organization=self.org, lead=self.lead, source=self.source):
                resolution = runtime.current_evidence_resolution(
                    organization_id=self.org.pk, lead_id=self.lead.pk,
                )
                self.assertIsNotNone(resolution)
                self.assertTrue(resolution.verified)
                context = service.context_builder.build(organization=self.org, lead=self.lead)
                payload = json.loads(service._build_input(context=context))
                self.assertTrue(payload["grounding"]["verified"])
                self.assertIn("₹999/month", str(payload["grounding"]))
                self.assertIsNone(conversation_policy_runtime._TURN.get())
                self.assertIs(
                    intent_runtime.current_intent_decision(
                        organization_id=self.org.pk, lead_id=self.lead.pk, source_message_id=self.source.pk,
                    ), self.intent,
                )
            self.assertEqual(classify.call_count, 1)
            self.assertEqual(classify.call_args.kwargs["message"], self.source.body)
            self.assertEqual(classify.call_args.kwargs["source_message_id"], str(self.source.pk))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes, before)

    def test_scope_is_restored_when_generation_raises(self):
        old_evidence = runtime._ACTIVE_EVIDENCE.set({"sentinel": "evidence"})
        old_memory = runtime._ACTIVE_MEMORY.set({"sentinel": "memory"})
        old_turn = conversation_policy_runtime._TURN.set({"sentinel": "turn"})
        old_policy = conversation_policy_runtime._POLICY.set(None)
        old_intent = intent_runtime._CURRENT.set({"sentinel": "intent"})
        try:
            with patch("apps.ai_engagement.services.intent_engine.IntentEngine.classify", return_value=self.intent):
                with self.assertRaisesRegex(RuntimeError, "generation failed"):
                    with runtime.source_evidence_context(organization=self.org, lead=self.lead, source=self.source):
                        self.assertIsNone(conversation_policy_runtime._TURN.get())
                        raise RuntimeError("generation failed")
            self.assertEqual(runtime._ACTIVE_EVIDENCE.get(), {"sentinel": "evidence"})
            self.assertEqual(runtime._ACTIVE_MEMORY.get(), {"sentinel": "memory"})
            self.assertEqual(conversation_policy_runtime._TURN.get(), {"sentinel": "turn"})
            self.assertEqual(intent_runtime._CURRENT.get(), {"sentinel": "intent"})
        finally:
            intent_runtime._CURRENT.reset(old_intent)
            conversation_policy_runtime._POLICY.reset(old_policy)
            conversation_policy_runtime._TURN.reset(old_turn)
            runtime._ACTIVE_MEMORY.reset(old_memory)
            runtime._ACTIVE_EVIDENCE.reset(old_evidence)

    def test_foreign_source_and_outbound_source_are_rejected_before_classification(self):
        foreign = Organization.objects.create(name="Foreign evidence")
        with patch("apps.ai_engagement.services.intent_engine.IntentEngine.classify") as classify:
            with self.assertRaises(TenantScopeError):
                with runtime.source_evidence_context(organization=foreign, lead=self.lead, source=self.source):
                    self.fail("Foreign evidence was accepted")
            self.source.direction = InstagramMessage.Direction.OUTBOUND
            with self.assertRaises(TenantScopeError):
                with runtime.source_evidence_context(organization=self.org, lead=self.lead, source=self.source):
                    self.fail("Outbound source was accepted")
            classify.assert_not_called()

    def test_source_from_another_conversation_cannot_authorize_this_lead(self):
        pipeline = self.lead.pipeline
        other = Lead.objects.create(
            organization=self.org, pipeline=pipeline, stage=pipeline.stages.first(),
            name="Other customer", phone="", lead_source="instagram",
        )
        with patch("apps.ai_engagement.services.intent_engine.IntentEngine.classify") as classify:
            with self.assertRaises(TenantScopeError):
                with runtime.source_evidence_context(organization=self.org, lead=other, source=self.source):
                    self.fail("Another customer's source was accepted")
            classify.assert_not_called()
