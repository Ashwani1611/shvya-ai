from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation.models import HostedAutomationJob
from apps.hosted_automation.tasks import process_hosted_ai_engagement_job_task
from apps.organizations.models import Organization


class HostedSourceHistoryGuardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Hosted history guard")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Hosted Sales",
            country_code="+91",
            phone_number="8700274739",
            ai_enabled=False,
        )
        self.stage = self.pipeline.stages.first()
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Sales",
            phone_number_id="+918700274739",
            display_phone_number="+918700274739",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="History Guard Lead",
            phone="+919876543210",
        )

    def _job(self, *, external_id, is_history):
        source = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=external_id,
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body="Hello",
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={"isHistory": is_history},
        )
        return HostedAutomationJob.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            source_message=source,
            available_at=timezone.now(),
        )

    @patch("apps.hosted_automation.tasks.hosted_ai_block_reason")
    def test_history_source_is_skipped_before_any_ai_execution_checks(self, block_reason):
        job = self._job(external_id="history-source", is_history=True)

        result = process_hosted_ai_engagement_job_task.run(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result, {"status": "skipped", "reason": "source_message_is_history"})
        self.assertEqual(job.status, HostedAutomationJob.Status.SKIPPED)
        self.assertEqual(job.result["reason"], "source_message_is_history")
        self.assertIsNotNone(job.completed_at)
        block_reason.assert_not_called()

    @patch(
        "apps.hosted_automation.tasks.hosted_ai_block_reason",
        return_value="downstream_test_block",
    )
    def test_live_promoted_source_continues_to_normal_execution(self, block_reason):
        job = self._job(external_id="live-source", is_history=False)

        result = process_hosted_ai_engagement_job_task.run(str(job.id))

        job.refresh_from_db()
        self.assertEqual(result, {"status": "skipped", "reason": "downstream_test_block"})
        self.assertEqual(job.status, HostedAutomationJob.Status.SKIPPED)
        self.assertEqual(job.result["reason"], "downstream_test_block")
        block_reason.assert_called_once_with(account=self.account, lead=self.lead)
