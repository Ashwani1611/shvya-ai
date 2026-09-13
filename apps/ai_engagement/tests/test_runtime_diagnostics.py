import os
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.ai_engagement.management.commands.check_ai_runtime import (
    configured_ai_models,
    recent_hosted_ai_jobs,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation.models import HostedAutomationJob
from apps.organizations.models import Organization


@override_settings(OPENAI_AI_MODEL="fallback-model")
class RuntimeModelDiagnosticsTests(SimpleTestCase):
    def test_effective_model_routing_uses_task_overrides_and_fallback(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_ENGAGEMENT_MODEL": "engagement-model",
                "OPENAI_QUALIFICATION_MODEL": "qualification-model",
                "OPENAI_SUMMARY_MODEL": "",
            },
            clear=False,
        ):
            models = configured_ai_models()

        self.assertEqual(models["engagement"], "engagement-model")
        self.assertEqual(models["qualification"], "qualification-model")
        self.assertEqual(models["internal_summary"], "fallback-model")


class HostedRuntimeDiagnosticsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Runtime Diagnostics Org",
            settings={"hosted_account_enabled": True},
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Hosted Sales",
            country_code="+91",
            phone_number="8700274739",
            ai_enabled=True,
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Runtime Lead",
            phone="+919876543210",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Runtime",
            phone_number_id="+918700274739",
            display_phone_number="+918700274739",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _message(self, external_id):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=external_id,
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body="diagnostic test message",
            status=WhatsAppMessage.Status.RECEIVED,
        )

    def _job(self, external_id, *, status, result=None, error="", available_at=None):
        return HostedAutomationJob.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            source_message=self._message(external_id),
            status=status,
            available_at=available_at or timezone.now(),
            result=result or {},
            error=error,
        )

    def test_hosted_diagnostics_report_status_reason_delivery_and_staleness(self):
        self._job(
            "runtime-completed",
            status=HostedAutomationJob.Status.COMPLETED,
            result={"status": "completed", "delivery": {"status": "sent"}},
        )
        self._job(
            "runtime-skipped",
            status=HostedAutomationJob.Status.SKIPPED,
            result={"status": "skipped", "reason": "stage_ai_disabled"},
        )
        self._job(
            "runtime-queued",
            status=HostedAutomationJob.Status.QUEUED,
            available_at=timezone.now() - timedelta(minutes=2),
        )
        self._job(
            "runtime-failed",
            status=HostedAutomationJob.Status.FAILED,
            result={"status": "failed", "reason": "unsafe free text must not leak"},
            error="provider detail that must never be printed",
        )

        inspected, counts = recent_hosted_ai_jobs(minutes=90, limit=50)

        self.assertEqual(inspected, 4)
        self.assertEqual(counts["status_completed"], 1)
        self.assertEqual(counts["delivery_sent"], 1)
        self.assertEqual(counts["status_skipped"], 1)
        self.assertEqual(counts["reason_stage_ai_disabled"], 1)
        self.assertEqual(counts["status_queued"], 1)
        self.assertEqual(counts["stale_queued"], 1)
        self.assertEqual(counts["status_failed"], 1)
        self.assertEqual(counts["failed_with_persisted_error"], 1)
        self.assertNotIn("reason_unsafe free text must not leak", counts)
