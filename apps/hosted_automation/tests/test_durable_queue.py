from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import AIMessageSendState, WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation.execution import _latest_for_account
from apps.hosted_automation.models import HostedAutomationJob
from apps.hosted_automation.tasks import _claim, _execute, _finish, process_hosted_ai_engagement_job_task
from apps.organizations.models import Organization
from services.channels.hosted_automation_service import (
    HostedAutomationPaused, _recover_stale_hosted_ai_jobs,
    dispatch_one_hosted_ai_job, enqueue_hosted_welcome, has_pending_ai,
)


class DurableHostedQueueTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Durable queue")
        self.pipeline = Pipeline.objects.create(
            organization=self.org, name="Sales", country_code="+91",
            phone_number="8700274739", ai_enabled=True,
        )
        self.stage = self.pipeline.stages.first()
        self.stage.ai_on = True
        self.stage.save(update_fields=["ai_on"])
        self.account = WhatsAppAccount.objects.create(
            organization=self.org, connection_type="hosted", business_name="Queue",
            phone_number_id="+918700274739", display_phone_number="+918700274739",
            status="connected", is_active=True,
        )
        self.org.settings = {"hosted_whatsapp": {"sessions": {str(self.account.pk): {"ai_auto_reply": True}}}}
        self.org.save(update_fields=["settings"])
        self.account.organization = self.org
        self.lead = self._lead("+919876543210")

    def _lead(self, phone):
        return Lead.objects.create(organization=self.org, pipeline=self.pipeline,
                                   stage=self.stage, name="Lead", phone=phone, ai_enabled=True)

    def _job(self, lead=None, account=None, text="Please help"):
        lead, account = lead or self.lead, account or self.account
        with patch("apps.ai_engagement.services.ai_permissions.AIPermissionService.evaluate",
                   return_value=SimpleNamespace(allowed=True, reason="allowed")):
            inbound = WhatsAppMessage.objects.create(
                organization=self.org, account=account, lead=lead, direction="inbound",
                from_number=lead.phone, to_number=account.display_phone_number,
                body=text, status="received",
            )
        job, _ = HostedAutomationJob.objects.get_or_create(
            source_message=inbound,
            defaults={"organization": self.org, "account": account, "lead": lead,
                      "available_at": timezone.now()},
        )
        HostedAutomationJob.objects.filter(pk=job.pk).update(available_at=timezone.now()-timedelta(seconds=1))
        job.refresh_from_db()
        return job

    def test_multiple_inbounds_persist_jobs_before_commit_or_broker_publish(self):
        first = self._job()
        second = self._job(self._lead("+919876543211"))
        third = self._job(text="And pricing?")
        self.assertEqual(HostedAutomationJob.objects.count(), 3)
        self.assertNotEqual(first.pk, third.pk)
        self.assertEqual(second.status, "queued")

    def test_newer_turn_inherits_first_burst_deadline(self):
        first = self._job()
        deadline = timezone.now() + timedelta(seconds=20)
        HostedAutomationJob.objects.filter(pk=first.pk).update(available_at=deadline)
        # The post-save enqueue must inherit the original wait, not add 45s.
        with patch("apps.ai_engagement.services.ai_permissions.AIPermissionService.evaluate",
                   return_value=SimpleNamespace(allowed=True, reason="allowed")):
            inbound = WhatsAppMessage.objects.create(
                organization=self.org, account=self.account, lead=self.lead,
                direction="inbound", from_number=self.lead.phone,
                to_number=self.account.display_phone_number, body="And availability?", status="received",
            )
        job = HostedAutomationJob.objects.get(source_message=inbound)
        self.assertLessEqual(job.available_at, deadline)

    def _bulk_welcomes(self, count):
        leads = [Lead(organization=self.org, pipeline=self.pipeline, stage=self.stage,
                      name=f"Bulk {index}", phone=f"+919888{index:06d}") for index in range(count)]
        Lead.objects.bulk_create(leads)
        HostedAutomationJob.objects.bulk_create([
            HostedAutomationJob(organization=self.org, account=self.account, lead=lead,
                                kind="welcome", available_at=timezone.now()) for lead in leads
        ])

    def test_cadence_pending_check_stops_at_first_eligible_bulk_job(self):
        self._bulk_welcomes(250)
        with patch("services.channels.hosted_automation_service.job_ai_block_reason", return_value="") as permission:
            self.assertTrue(has_pending_ai(account=self.account))
        permission.assert_called_once()
        self.assertEqual(HostedAutomationJob.objects.filter(status="queued").count(), 250)

    def test_cadence_pending_cleanup_is_bounded_and_keeps_remaining_jobs_pending(self):
        self._bulk_welcomes(101)
        with patch("services.channels.hosted_automation_service.job_ai_block_reason", return_value="stage_ai_disabled") as permission:
            self.assertTrue(has_pending_ai(account=self.account))
        self.assertEqual(permission.call_count, 100)
        self.assertEqual(HostedAutomationJob.objects.filter(status="queued").count(), 1)
        self.assertEqual(HostedAutomationJob.objects.filter(status="skipped").count(), 100)

    def test_cadence_pending_check_leaves_active_processing_lease_to_worker(self):
        job = self._job()
        claimed, _ = _claim(job.pk)
        with patch("services.channels.hosted_automation_service.job_ai_block_reason") as permission:
            self.assertTrue(has_pending_ai(account=self.account))
        permission.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "processing")
        self.assertEqual(job.claim_token, claimed.claim_token)

    def test_welcome_is_unique_intent_without_transcript_row(self):
        first = enqueue_hosted_welcome(account=self.account, lead=self.lead)
        again = enqueue_hosted_welcome(account=self.account, lead=self.lead)
        self.assertEqual(first.pk, again.pk)
        self.assertIsNone(first.source_message_id)
        self.assertFalse(WhatsAppMessage.objects.filter(direction="outbound").exists())

    def test_welcome_has_priority_over_earlier_reply(self):
        reply = self._job()
        welcome = enqueue_hosted_welcome(account=self.account, lead=self._lead("+919876543211"))
        with patch("services.channels.hosted_automation_service.job_ai_block_reason", return_value=""), patch(
            "services.channels.hosted_automation_service.automation_pause_until", return_value=None,
        ):
            result = dispatch_one_hosted_ai_job()
        self.assertEqual(result["job_id"], str(welcome.pk))
        reply.refresh_from_db()
        welcome.refresh_from_db()
        self.assertIsNone(reply.lease_expires_at)
        self.assertGreater(welcome.lease_expires_at, timezone.now())
        self.assertEqual(welcome.status, "queued")

    def test_one_account_cannot_claim_two_jobs_concurrently(self):
        first = self._job()
        second = self._job(self._lead("+919876543211"))
        claimed, _ = _claim(first.pk)
        blocked, result = _claim(second.pk)
        self.assertIsNotNone(claimed)
        self.assertIsNone(blocked)
        self.assertEqual(result["reason"], "account_processing")
        first.refresh_from_db()
        self.assertEqual(first.status, "processing")
        self.assertTrue(first.claim_token)

    def test_dispatch_batch_wakes_other_accounts_without_global_redis_lock(self):
        first = self._job()
        account = WhatsAppAccount.objects.create(
            organization=self.org, connection_type="hosted", business_name="Other sender",
            phone_number_id="+918700274740", display_phone_number="+918700274740",
            status="connected", is_active=True,
        )
        second = self._job(self._lead("+919876543212"), account=account)
        with patch("services.channels.hosted_automation_service.job_ai_block_reason", return_value=""), patch(
            "services.channels.hosted_automation_service.automation_pause_until", return_value=None,
        ), patch("services.channels.hosted_automation_service.cache.add", side_effect=RuntimeError("Redis unavailable")):
            result = dispatch_one_hosted_ai_job()
        self.assertCountEqual(result["job_ids"], [str(first.pk), str(second.pk)])

    def test_broker_publish_failure_preserves_queue_and_releases_dispatch_lease(self):
        job = self._job()
        with patch("services.channels.hosted_automation_service.job_ai_block_reason", return_value=""), patch(
            "services.channels.hosted_automation_service.automation_pause_until", return_value=None,
        ), patch("apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task.delay",
                 side_effect=RuntimeError("broker unavailable")), self.captureOnCommitCallbacks(execute=True):
            dispatch_one_hosted_ai_job()
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertIsNone(job.lease_expires_at)
        self.assertIsNone(job.started_at)

    def test_ready_account_is_not_starved_by_over_100_blocked_fifo_heads(self):
        # Each blocked sender has a delayed welcome ahead of a due reply.
        # Scanning arbitrary due jobs instead of each account's head would
        # repeatedly consume the whole batch without reaching the ready one.
        self._job()
        ready = enqueue_hosted_welcome(account=self.account, lead=self.lead)
        blocked_accounts = [WhatsAppAccount(
            organization=self.org, connection_type="hosted", business_name=f"Blocked {i}",
            phone_number_id=f"blocked-{i}", display_phone_number=f"+91900000{i:04d}",
            status="connected", is_active=True,
        ) for i in range(101)]
        WhatsAppAccount.objects.bulk_create(blocked_accounts)
        now = timezone.now()
        jobs = []
        for account in blocked_accounts:
            jobs.extend([
                HostedAutomationJob(organization=self.org, account=account, lead=self.lead,
                                    kind="welcome", available_at=now+timedelta(hours=1)),
                HostedAutomationJob(organization=self.org, account=account, lead=self.lead,
                                    available_at=now-timedelta(hours=1)),
            ])
        HostedAutomationJob.objects.bulk_create(jobs)
        with patch("services.channels.hosted_automation_service.job_ai_block_reason", return_value=""), patch(
            "services.channels.hosted_automation_service.automation_pause_until", return_value=None,
        ):
            result = dispatch_one_hosted_ai_job()
        self.assertEqual(result["job_ids"], [str(ready.pk)])

    def test_sender_gate_prevents_early_generation(self):
        job = self._job()
        due = timezone.now() + timedelta(seconds=45)
        AIMessageSendState.objects.create(account=self.account, next_send_at=due)
        with patch("apps.hosted_automation.execution.execute_hosted_ai_engagement") as generate:
            result = process_hosted_ai_engagement_job_task.run(str(job.pk))
        self.assertEqual(result["reason"], "not_due")
        generate.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.available_at, due)

    def test_new_inbound_during_generation_keeps_newest_turn_queued(self):
        first = self._job()
        claimed, _ = _claim(first.pk)
        newest = self._job(text="New question while model runs")
        result = _execute(claimed)
        self.assertEqual(result["reason"], "superseded_by_newer_lead_message")
        with patch("apps.hosted_automation.tasks.hosted_ai_block_reason", return_value=""), patch(
            "apps.hosted_automation.tasks.hosted_health_pause_until", return_value=None,
        ), patch("apps.hosted_automation.execution.execute_hosted_ai_engagement",
                 return_value={"status": "completed", "reason": "no_engagement"}):
            process_hosted_ai_engagement_job_task.run(str(newest.pk))
        newest.refresh_from_db()
        self.assertEqual(newest.status, "completed")

    def test_superseded_job_cancels_orphaned_draft_before_dispatching_new_turn(self):
        old = self._job()
        draft = WhatsAppMessage.objects.create(
            organization=self.org, account=self.account, lead=self.lead,
            direction="outbound", status="queued", body="Old response",
            raw_payload={"shvya_ai": {"source_inbound_message_id": str(old.source_message_id)}},
        )
        newest = self._job(text="Updated request")
        with patch("services.channels.hosted_automation_service.job_ai_block_reason", return_value=""), patch(
            "services.channels.hosted_automation_service.automation_pause_until", return_value=None,
        ):
            result = dispatch_one_hosted_ai_job()
        self.assertEqual(result["job_id"], str(newest.pk))
        old.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(old.status, "skipped")
        self.assertEqual(draft.status, "failed")

    def test_provider_retry_returns_to_queue_instead_of_processing(self):
        job = self._job()
        pause = HostedAutomationPaused(timezone.now()+timedelta(seconds=30))
        pause.reason = "provider_transient"
        with patch("apps.hosted_automation.tasks.hosted_ai_block_reason", return_value=""), patch(
            "apps.hosted_automation.tasks.hosted_health_pause_until", return_value=None,
        ), patch("apps.hosted_automation.execution.execute_hosted_ai_engagement", side_effect=pause):
            result = process_hosted_ai_engagement_job_task.run(str(job.pk))
        self.assertEqual(result["status"], "deferred")
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.lease_expires_at)
        self.assertEqual(job.claim_token, "")
        self.assertEqual(job.result["retry_count"], 1)

    def test_repeated_provider_failures_stop_at_bounded_limit(self):
        job = self._job()
        job.result = {"retry_count": 8}
        job.save(update_fields=["result"])
        with patch("apps.hosted_automation.tasks.hosted_ai_block_reason", return_value=""), patch(
            "apps.hosted_automation.tasks.hosted_health_pause_until", return_value=None,
        ), patch("apps.hosted_automation.execution.execute_hosted_ai_engagement", side_effect=RuntimeError("provider unavailable")):
            result = process_hosted_ai_engagement_job_task.run(str(job.pk))
        self.assertEqual(result["reason"], "retry_limit_exceeded")
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIsNone(job.lease_expires_at)

    def test_expired_worker_is_fenced_from_finalizing_replacement(self):
        job = self._job()
        old, _ = _claim(job.pk)
        HostedAutomationJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(_recover_stale_hosted_ai_jobs(now=timezone.now()), 1)
        new, _ = _claim(job.pk)
        self.assertNotEqual(old.claim_token, new.claim_token)
        result = _finish(old, HostedAutomationJob.Status.COMPLETED, {"status": "completed"})
        self.assertEqual(result["reason"], "job_lease_replaced")
        job.refresh_from_db()
        self.assertEqual(job.claim_token, new.claim_token)
        self.assertEqual(job.status, "processing")

    def test_welcome_does_not_consume_customer_inbound_but_human_reply_does(self):
        job = self._job()
        WhatsAppMessage.objects.create(
            organization=self.org, account=self.account, lead=self.lead,
            direction="outbound", status="sent", body="Welcome!",
            raw_payload={"shvya_welcome": {"trigger": "lead_created"}},
        )
        self.assertEqual(_latest_for_account(lead=self.lead, account=self.account).pk, job.source_message_id)
        human = WhatsAppMessage.objects.create(
            organization=self.org, account=self.account, lead=self.lead,
            direction="outbound", status="sent", body="I will assist you personally.",
        )
        self.assertEqual(_latest_for_account(lead=self.lead, account=self.account).pk, human.pk)
