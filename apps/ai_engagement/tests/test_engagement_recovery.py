import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.ai_engagement.services.ai_provider import (
    AIProviderConfigurationError, AITextResult, OpenAIProvider,
)
from apps.ai_engagement.services.engagement import (
    ENGAGEMENT_RESPONSE_SCHEMA, EngagementError, EngagementService,
)
from apps.ai_engagement.tests import test_precise_orchestration as fixtures


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class EngagementRecoveryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.context = fixtures.PreciseEngagementTests()._context("I live in Delhi")
        self.context.organization["qualification_requirements"] = "Which city?\nWhat is your budget?"
        self.context.conversation["messages"][0]["id"] = "inbound-1"
        self.valid = {
            "should_engage": True, "message": "What is your budget?",
            "file_document_id": None, "crm_actions": [],
            "qualification_updates": [{
                "requirement_id": "which_city", "value": "Delhi",
                "source_message_id": "inbound-1", "evidence": "Delhi",
            }],
            "next_requirement_id": "what_is_your_budget", "reason_code": "QUALIFICATION_NEXT",
        }

    def _engage(self, outputs):
        provider = Mock()
        provider.generate_text.side_effect = [AITextResult(json.dumps(item), "test") for item in outputs]
        with patch("apps.ai_engagement.services.engagement.EngagementGenerationLock") as lock:
            lock.return_value.acquire.return_value = True
            result = EngagementService(provider=provider).engage(
                organization=SimpleNamespace(id="org-1"), lead=SimpleNamespace(id="lead-1"),
                context=self.context,
            )
        return result, provider

    def test_wrong_next_question_is_repaired_using_original_evidence(self):
        invalid = {**self.valid, "next_requirement_id": "which_city", "message": "Which city?"}
        result, provider = self._engage([invalid, self.valid])
        self.assertEqual(result.message, "What is your budget?")
        self.assertEqual(provider.generate_text.call_count, 2)
        repair = provider.generate_text.call_args.kwargs
        self.assertEqual(repair["metadata"]["phase"], "schema_repair")
        turn = json.loads(repair["input_text"])["original_turn"]
        self.assertEqual(turn["recent_conversation"]["messages"][0]["body"], "I live in Delhi")
        self.assertEqual(result.qualification_updates[0]["value"], "Delhi")

    def test_unsupported_evidence_can_be_corrected_without_losing_reply(self):
        invalid = {**self.valid, "qualification_updates": [{
            **self.valid["qualification_updates"][0], "evidence": "Mumbai", "value": "Mumbai",
        }]}
        result, provider = self._engage([invalid, self.valid])
        self.assertEqual(result.qualification_updates[0]["value"], "Delhi")
        self.assertEqual(provider.generate_text.call_count, 2)

    def test_invalid_repair_never_caches_or_persists_unsupported_answers(self):
        invalid = {**self.valid, "next_requirement_id": "invented_requirement"}
        with self.assertRaises(EngagementError):
            self._engage([invalid, invalid])
        self.assertIsNone(cache.get("shvya:ai:decision:org-1:lead-1:inbound-1"))

    def test_provider_initialization_failure_releases_turn_claim(self):
        with (
            patch("apps.ai_engagement.services.engagement.EngagementGenerationLock") as lock,
            patch("apps.ai_engagement.services.engagement.OpenAIProvider",
                  side_effect=AIProviderConfigurationError("missing key")),
        ):
            lock.return_value.acquire.return_value = True
            with self.assertRaises(AIProviderConfigurationError):
                EngagementService().engage(
                    organization=SimpleNamespace(id="org-1"), lead=SimpleNamespace(id="lead-1"),
                    context=self.context,
                )
            lock.return_value.finish.assert_called_once_with(success=False)


@override_settings(OPENAI_API_KEY="test-key")
class StructuredReplyBudgetTests(SimpleTestCase):
    @patch.dict(os.environ, {"OPENAI_ENGAGEMENT_MAX_OUTPUT_TOKENS": "300"})
    def test_legacy_budget_cannot_truncate_structured_reply_and_repair(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text="{}", model="test")
        provider = OpenAIProvider(client=client)
        for phase, expected in [("primary", 700), ("schema_repair", 1400)]:
            provider.generate_text(
                instructions="Test", input_text="Test",
                metadata={"task": "engagement", "phase": phase},
                response_schema=ENGAGEMENT_RESPONSE_SCHEMA,
            )
            self.assertEqual(client.responses.create.call_args.kwargs["max_output_tokens"], expected)
        provider.generate_text(instructions="Test", input_text="Test", metadata={"task": "engagement"})
        self.assertEqual(client.responses.create.call_args.kwargs["max_output_tokens"], 300)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class MultiTurnTransportRecoveryTests(TestCase):
    """Exercise the real graph and finalizer on both transports, without sending."""

    def _conversation(self, transport):
        from apps.ai_engagement.models import OrgInfo
        from apps.ai_engagement.services.qualification_state import state_for_lead
        from apps.ai_engagement.tasks import generate_ai_engagement_response
        from apps.channels.models import WhatsAppAccount, WhatsAppMessage
        from apps.crm.models import Lead, Pipeline, Stage
        from apps.hosted_automation.models import HostedAutomationJob
        from apps.hosted_automation.tasks import process_hosted_ai_engagement_job_task
        from apps.organizations.models import Organization

        cache.clear()
        org = Organization.objects.create(name=f"Recovery {transport}")
        pipeline = Pipeline.objects.create(
            organization=org, name="Sales", country_code="+91", phone_number="9876543210",
            ai_enabled=True,
        )
        stage = Stage.objects.create(pipeline=pipeline, name="New Lead", display_order=0, ai_on=True)
        lead = Lead.objects.create(
            organization=org, pipeline=pipeline, stage=stage,
            name="Test Customer", phone="+919111111111", ai_enabled=True,
        )
        account = WhatsAppAccount.objects.create(
            organization=org, connection_type=transport, display_phone_number="+919876543210",
            phone_number_id=f"test-{transport}", status="connected", is_active=True,
        )
        questions = ["Which city?", "What occupation?", "Which product?", "Which color?"]
        requirement_ids = ["which_city", "what_occupation", "which_product", "which_color"]
        OrgInfo.objects.update_or_create(organization=org, defaults={
            "qualification_requirements": "\n".join(questions), "bot_languages": "English",
        })
        replies = ["Hello", "Delhi", "Teacher", "Desk", "Blue", "Thank you"]

        def deliver(*, message, **kwargs):
            message.status = "sent"
            message.save(update_fields=["status"])

        with (
            patch("apps.ai_engagement.services.engagement.OpenAIProvider") as provider_class,
            patch("apps.ai_engagement.services.engagement.EngagementGenerationLock") as lock,
            patch("apps.ai_engagement.background_signals.queue_background_enrichment"),
            patch("services.channels.hosted_whatsapp_transport.send_hosted_message", side_effect=deliver) as hosted_send,
            patch("apps.channels.tasks.send_whatsapp_message_task.delay") as api_send,
        ):
            lock.return_value.acquire.return_value = True
            provider = provider_class.return_value
            for index, reply in enumerate(replies):
                inbound = WhatsAppMessage.objects.create(
                    organization=org, account=account, lead=lead, direction="inbound",
                    external_id=f"{transport}-turn-{index}", body=reply, status="received",
                    from_number=lead.phone, to_number=account.display_phone_number,
                )
                next_id = requirement_ids[index] if index < len(requirement_ids) else None
                final_qualification_answer = index == len(requirement_ids)
                response_message = (
                    questions[index]
                    if next_id
                    else (
                        "Thank you, I have all the required details."
                        if final_qualification_answer
                        else "How else can I help?"
                    )
                )
                expected = {
                    "should_engage": True,
                    "message": response_message,
                    "file_document_id": None, "crm_actions": [],
                    "qualification_updates": [{
                        "requirement_id": requirement_ids[index - 1], "value": reply,
                        "source_message_id": str(inbound.id), "evidence": reply,
                    }] if 1 <= index <= len(requirement_ids) else [],
                    "next_requirement_id": next_id,
                    "reason_code": "QUALIFICATION_NEXT" if next_id else "NORMAL_CONVERSATION",
                }
                # A semantic error after the third exchange must be repaired;
                # it must not stop this turn or any subsequent inbound turn.
                outputs = [expected]
                if index == 3:
                    outputs.insert(0, {**expected, "next_requirement_id": "which_product"})
                provider.generate_text.side_effect = [
                    AITextResult(json.dumps(output), "test") for output in outputs
                ]
                with self.captureOnCommitCallbacks(execute=True):
                    if transport == "hosted":
                        job = HostedAutomationJob.objects.create(
                            organization=org, account=account, lead=lead, source_message=inbound,
                            available_at=timezone.now(),
                        )
                        result = process_hosted_ai_engagement_job_task.run(str(job.id))
                        job.refresh_from_db()
                        self.assertEqual(job.status, "completed", job.result)
                    else:
                        result = generate_ai_engagement_response.run(str(lead.id))
                self.assertEqual(result["status"], "completed", result)
                outbound = WhatsAppMessage.objects.get(pk=result["message_id"])
                self.assertEqual(outbound.account_id, account.id)
                self.assertEqual(outbound.body, expected["message"])
                self.assertEqual(outbound.raw_payload["shvya_ai"]["source_inbound_message_id"], str(inbound.id))
                if transport == "api":
                    deliver(message=outbound)

            self.assertEqual(provider.generate_text.call_count, 7)
            self.assertEqual(api_send.call_count, 6)
            self.assertEqual(hosted_send.call_count, 6 if transport == "hosted" else 0)
            self.assertEqual(lead.whatsapp_messages.filter(direction="outbound").count(), 6)
            lead.refresh_from_db()
            self.assertTrue(lead.ai_enabled)
            state = state_for_lead(lead)
            self.assertEqual(state["requirement_states"]["which_product"]["value"], "Desk")

    def test_api_keeps_replying_after_third_turn_and_qualification(self):
        self._conversation("api")

    def test_hosted_keeps_replying_after_third_turn_and_qualification(self):
        self._conversation("hosted")
