from unittest.mock import Mock, patch

from celery.exceptions import Retry
from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import AIProviderTransientError
from apps.ai_engagement.services.engagement import EngagementError
from apps.ai_engagement.services.engagement_failsoft import build_deterministic_fallback_decision
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.tests import test_engagement_controls as controls
from apps.channels.models import WhatsAppMessage


class TerminalEngagementFailsoftTests(TestCase):
    setUp = controls.AIEngagementControlTests.setUp
    _inbound = controls.AIEngagementControlTests._inbound

    @staticmethod
    def _permanent_failure(*args, **kwargs):
        raise EngagementError("Permanent provider/schema validation failure")

    def test_permanent_generation_error_queues_neutral_safe_reply(self):
        source = self._inbound()
        with patch(
            "apps.ai_engagement.services.engagement.EngagementService.engage",
            side_effect=self._permanent_failure,
        ), patch("apps.channels.tasks.send_whatsapp_message_task.delay") as send:
            with self.captureOnCommitCallbacks(execute=True):
                from apps.ai_engagement.tasks import _execute_ai_engagement_response_impl
                result = _execute_ai_engagement_response_impl(task=Mock(), lead_id=str(self.lead.pk))

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["engaged"])
        self.assertEqual(result["model"], "deterministic-fallback")
        outbound = WhatsAppMessage.objects.get(pk=result["message_id"])
        self.assertEqual(
            outbound.body,
            "Thanks for your message. I’ve received it. Please share the specific detail you’d like help with.",
        )
        self.assertEqual(outbound.raw_payload["shvya_ai"]["source_inbound_message_id"], str(source.id))
        send.assert_called_once_with(str(outbound.id))

    def test_permanent_generation_error_uses_exact_authored_qualification_question(self):
        raw = "What is your budget? A) Under 10k B) Above 10k?"
        OrgInfo.objects.create(organization=self.organization, qualification_requirements=raw)
        expected = compile_qualification_requirements(raw)["requirements"][0]
        self._inbound(external_id="wamid-fallback-qualification")

        with patch(
            "apps.ai_engagement.services.engagement.EngagementService.engage",
            side_effect=self._permanent_failure,
        ), patch("apps.channels.tasks.send_whatsapp_message_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                from apps.ai_engagement.tasks import _execute_ai_engagement_response_impl
                result = _execute_ai_engagement_response_impl(task=Mock(), lead_id=str(self.lead.pk))

        self.assertEqual(result["status"], "completed")
        outbound = WhatsAppMessage.objects.get(pk=result["message_id"])
        self.assertEqual(outbound.body, expected["question"])
        self.assertEqual(outbound.raw_payload["shvya_ai"]["model"], "deterministic-fallback")
        self.assertEqual(outbound.raw_payload["shvya_ai"]["next_requirement_id"], expected["id"])

    def test_transient_provider_failure_still_retries_instead_of_fallback(self):
        self._inbound(external_id="wamid-transient")
        transient = AIProviderTransientError("temporary provider outage")
        wrapped = EngagementError("AI engagement generation failed")
        wrapped.__cause__ = transient
        task = Mock()
        task.retry.side_effect = Retry()

        with patch(
            "apps.ai_engagement.services.engagement.EngagementService.engage",
            side_effect=wrapped,
        ), patch(
            "apps.ai_engagement.services.engagement_failsoft.build_deterministic_fallback_decision"
        ) as fallback:
            from apps.ai_engagement.tasks import _execute_ai_engagement_response_impl
            with self.assertRaises(Retry):
                _execute_ai_engagement_response_impl(task=task, lead_id=str(self.lead.pk))

        fallback.assert_not_called()
        task.retry.assert_called_once()

    def test_fallback_builder_does_not_touch_ai_context_or_provider(self):
        self._inbound(external_id="wamid-no-context")
        with patch(
            "apps.ai_engagement.services.context.AIContextBuilder.build",
            side_effect=AssertionError("AI context must not be used by fail-soft"),
        ):
            decision = build_deterministic_fallback_decision(
                organization=self.organization,
                lead=self.lead,
            )

        self.assertTrue(decision.should_engage)
        self.assertEqual(decision.model, "deterministic-fallback")
        self.assertEqual(decision.crm_actions, [])
        self.assertIsNone(decision.file_document_id)
