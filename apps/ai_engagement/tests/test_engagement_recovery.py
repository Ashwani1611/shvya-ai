import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.qualification_state import state_for_lead
from apps.ai_engagement.tasks import generate_ai_engagement_response
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation.models import HostedAutomationJob
from apps.hosted_automation.tasks import process_hosted_ai_engagement_job_task
from apps.organizations.models import Organization


class MultiTurnTransportRecoveryTests(TestCase):
    def setUp(self):
        cache.clear()

    def _conversation(self, transport):
        org = Organization.objects.create(name=f"{transport}-recovery")
        pipeline = Pipeline.objects.create(
            organization=org,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
            ai_enabled=True,
        )
        stage = pipeline.stages.get(name="New leads")
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
            # API replies use the generic sender task. Hosted replies are sent
            # directly by the durable Hosted job so Account Health/pacing stays
            # provider-specific and no process-global sender interception exists.
            self.assertEqual(api_send.call_count, 6 if transport == "api" else 0)
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
