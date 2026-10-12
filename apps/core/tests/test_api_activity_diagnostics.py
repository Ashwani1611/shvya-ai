from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.test import SimpleTestCase

from apps.ai_engagement.activity_signals import ai_usage_settled
from apps.ai_engagement.models import AICreditTransaction
from apps.core.api_activity.operational import observe_operational_event
from apps.core.api_activity.services import activity_context


class ActivityDiagnosticsPrivacyTests(SimpleTestCase):
    @patch("apps.core.api_activity.operational.record_event")
    def test_application_diagnostics_link_without_arbitrary_content(self, record):
        identifier = str(uuid.uuid4())
        with activity_context({"activity_id": identifier}):
            observe_operational_event("ai.provider.failed", level="ERROR", fields={
                "model": "configured-model", "exception": "Timeout",
                "prompt": "private prompt", "response": "private response",
                "headers": {"Authorization": "secret"},
            })
        self.assertEqual(record.call_args.args, (identifier,))
        self.assertEqual(record.call_args.kwargs["metadata"], {
            "level": "ERROR", "model": "configured-model", "exception": "Timeout",
        })

    @patch("apps.ai_engagement.activity_signals.record_activity")
    def test_usage_observation_keeps_accounting_units_and_excludes_private_metadata(self, record):
        transaction = SimpleNamespace(
            pk=99, organization_id=uuid.uuid4(), transaction_type="ai_usage",
            model="configured-model", feature="engagement", input_tokens=42,
            output_tokens=10, amount=-7, reservation_id=uuid.uuid4(),
            description="private description", metadata={"prompt": "private prompt"},
        )
        ai_usage_settled(AICreditTransaction, transaction, created=True)
        data = record.call_args.kwargs
        self.assertEqual(data["organization_id"], transaction.organization_id)
        self.assertEqual(data["metadata"]["credits_consumed"], 7)
        self.assertEqual(data["metadata"]["cost_unit"], "ai_credits")
        self.assertEqual(data["metadata"]["input_tokens"], 42)
        self.assertNotIn("private", repr(data))
        self.assertEqual(transaction.amount, -7)
        record.reset_mock()
        transaction.transaction_type = "manual_credit"
        ai_usage_settled(AICreditTransaction, transaction, created=True)
        record.assert_not_called()
