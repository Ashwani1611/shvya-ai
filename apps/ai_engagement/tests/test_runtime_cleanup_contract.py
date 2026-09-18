from __future__ import annotations

import inspect
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement import runtime_bootstrap
from services.channels import hosted_automation_service, whatsapp_service


class RuntimeCleanupContractTests(SimpleTestCase):
    def test_bootstrap_has_no_removed_compatibility_or_channel_hooks(self):
        source = inspect.getsource(runtime_bootstrap.install_ai_runtime)
        for legacy in (
            "canonical_architecture_compat",
            "conditional_state_postfix",
            "pipeline_context_guard",
            "qualification_grounding_scope_guard",
            "natural_conversation_booking_guard",
            "final_reply_guard",
            "ai_setup_runtime_compat",
            "ai_orchestration_hooks",
        ):
            self.assertNotIn(legacy, source)

    @patch("apps.ai_engagement.services.execution_tracker.queue_api_engagement")
    def test_api_queue_uses_durable_execution_tracker_directly(self, queue):
        queue.return_value = {"status": "queued"}

        result = whatsapp_service._queue_whatsapp_engagement(lead_id="lead-123")

        queue.assert_called_once_with(lead_id="lead-123")
        self.assertEqual(result, {"status": "queued"})

    def test_summary_queue_is_signal_owned(self):
        self.assertIsNone(
            whatsapp_service._queue_internal_conversation_summary(
                lead_id="lead-123"
            )
        )

    def test_hosted_ai_delay_is_bounded_without_startup_patch(self):
        self.assertGreaterEqual(hosted_automation_service.AI_RESPONSE_DELAY_SECONDS, 0)
        self.assertLessEqual(hosted_automation_service.AI_RESPONSE_DELAY_SECONDS, 5)
