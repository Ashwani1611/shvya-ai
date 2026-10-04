"""Hosted status follows the exact inbound job, independently of API markers."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.ai_engagement.models import AITrace
from apps.ai_engagement.services.diagnostics import (
    _hosted_job_snapshot,
    diagnose_engagement,
)
from apps.channels.ai_reply_status_ui import ai_reply_status
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation.models import HostedAutomationJob
from apps.organizations.models import Organization


class HostedJobSnapshotTests(SimpleTestCase):
    def _job(self, *, status="skipped", result=None):
        return SimpleNamespace(
            id=uuid4(), source_message_id=uuid4(), status=status,
            attempts=2, available_at=timezone.now(), updated_at=timezone.now(),
            result=result, error="private provider exception",
        )

    def test_fixed_worker_reason_is_retained_without_result_or_error(self):
        job = self._job(result={"reason": "stage_ai_disabled", "message": "private body"})
        snapshot = _hosted_job_snapshot(job)
        self.assertEqual(snapshot["status"], "skipped")
        self.assertEqual(snapshot["reason"], "stage_ai_disabled")
        self.assertEqual(snapshot["source_message_id"], str(job.source_message_id))
        self.assertEqual(snapshot["attempts"], 2)
        self.assertNotIn("private", json.dumps(snapshot))
        self.assertNotIn("result", snapshot)
        self.assertNotIn("error", snapshot)

    def test_free_form_or_code_shaped_provider_errors_are_never_exposed(self):
        for reason in (
            "Authorization failed with Bearer private-token",
            "sk_private_provider_token", {"reason": "private"}, ["private"],
        ):
            with self.subTest(reason=reason):
                snapshot = _hosted_job_snapshot(self._job(result={"reason": reason}))
                self.assertEqual(snapshot["reason"], "")
                self.assertNotIn("private", json.dumps(snapshot))

    def test_non_object_result_does_not_break_status(self):
        for result in (None, [], "private provider exception"):
            with self.subTest(result=result):
                self.assertEqual(_hosted_job_snapshot(self._job(result=result))["reason"], "")

    def test_queued_job_shows_current_deferral_instead_of_old_reason(self):
        snapshot = _hosted_job_snapshot(self._job(status="queued", result={
            "reason": "stage_ai_disabled", "defer_reason": "retry_scheduled",
        }))
        self.assertEqual(snapshot["status"], "queued")
        self.assertEqual(snapshot["reason"], "retry_scheduled")

    def test_terminal_job_does_not_show_stale_deferral(self):
        snapshot = _hosted_job_snapshot(self._job(status="completed", result={
            "reason": "no_engagement", "defer_reason": "retry_scheduled",
        }))
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["reason"], "no_engagement")


@override_settings(OPENAI_API_KEY="test-key-never-sent", CELERY_TASK_ALWAYS_EAGER=False)
class HostedReplyDiagnosticsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Hosted diagnostics")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization, name="Hosted sales",
            country_code="+91", phone_number="9876543210", ai_enabled=True,
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.stage.ai_on = True
        self.stage.save(update_fields=["ai_on"])
        self.lead = Lead.objects.create(
            organization=self.organization, pipeline=self.pipeline, stage=self.stage,
            name="Hosted customer", phone="+919111111111", ai_enabled=True,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization, connection_type="hosted",
            display_phone_number="+919876543210", status="connected", is_active=True,
        )

    def _message(self, *, account=None, direction="inbound", payload=None):
        account = account or self.account
        # Test durable rows explicitly; setup must not dispatch automation.
        with patch("apps.hosted_automation.signals._queue_hosted_ai_from_persisted_message"):
            return WhatsAppMessage.objects.create(
                organization=self.organization, account=account, lead=self.lead,
                direction=direction, external_id=f"diagnostic-{uuid4()}",
                from_number=self.lead.phone, to_number=account.display_phone_number,
                body="Diagnostic message", status="received" if direction == "inbound" else "sent",
                raw_payload=payload or {},
            )

    def _job(self, source, *, status="skipped", reason="stage_ai_disabled"):
        return HostedAutomationJob.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            source_message=source, status=status, available_at=timezone.now(), attempts=1,
            result={"status": status, "reason": reason},
        )

    def _response(self, *, organization=None):
        request = RequestFactory().get("/dashboard/whatsapp/ai-status/")
        request.crm_user = get_user_model().objects.create_user(
            email=f"status-{uuid4()}@example.com", password="test-only",
            organization=organization or self.organization,
        )
        request.user = request.crm_user
        return ai_reply_status.__wrapped__(request, lead_id=self.lead.pk)

    def test_latest_source_job_wins_even_when_older_job_was_created_later(self):
        older = self._message()
        latest = self._message()
        latest_job = self._job(latest, status="completed", reason="no_engagement")
        older_job = self._job(older)
        HostedAutomationJob.objects.filter(pk=older_job.pk).update(
            created_at=timezone.now() + timedelta(minutes=1),
        )
        report = diagnose_engagement(lead=self.lead)
        self.assertEqual(report["hosted_job"]["id"], str(latest_job.pk))
        self.assertEqual(report["execution"]["source_message_id"], str(latest.pk))
        self.assertEqual(report["execution"]["status"], "completed")
        self.assertEqual(report["execution"]["reason"], "no_engagement")

    def test_new_incoming_message_without_job_does_not_show_older_job(self):
        older = self._message()
        old_job = self._job(older)
        latest = self._message()
        report = diagnose_engagement(lead=self.lead)
        self.assertNotIn("hosted_job", report)
        self.assertEqual(report["execution"]["status"], "not_queued")
        self.assertEqual(report["execution"]["source_message_id"], str(latest.pk))
        response = self._response()
        self.assertContains(response, "No Hosted AI reply queued for the latest incoming message")
        self.assertNotContains(response, "Hosted reply: skipped")
        self.assertNotContains(response, str(old_job.pk))

    def test_latest_history_import_without_job_explains_pre_enqueue_skip(self):
        older = self._message()
        old_job = self._job(older, status="completed", reason="no_engagement")
        latest = self._message(payload={"isHistory": True, "private_payload": "must not appear"})
        report = diagnose_engagement(lead=self.lead)
        self.assertNotIn("hosted_job", report)
        self.assertIs(report["latest_inbound_message"]["is_history"], True)
        self.assertEqual(report["execution"]["status"], "history_import")
        self.assertEqual(report["execution"]["reason"], "source_message_is_history")
        self.assertEqual(report["execution"]["source_message_id"], str(latest.pk))
        self.assertNotIn("no_hosted_ai_job_check_live_inbound_and_lead_mapping", report["blockers"])
        self.assertNotIn("private_payload", json.dumps(report))
        response = self._response()
        self.assertContains(response, "Historical import; no AI reply queued")
        self.assertContains(response, "imported from WhatsApp history")
        self.assertNotContains(response, "Check live-message and lead mapping")
        self.assertNotContains(response, str(old_job.pk))

    def test_explicitly_activated_history_job_retains_its_durable_status(self):
        source = self._message(payload={"isHistory": True})
        job = self._job(source, status="queued", reason="")
        report = diagnose_engagement(lead=self.lead)
        self.assertIs(report["latest_inbound_message"]["is_history"], True)
        self.assertEqual(report["hosted_job"]["id"], str(job.pk))
        self.assertEqual(report["execution"]["status"], "queued")
        self.assertNotIn("source_message_is_history", report["blockers"])
        response = self._response()
        self.assertContains(response, "Waiting for the AI worker")
        self.assertNotContains(response, "Historical import; no AI reply queued")

    def test_only_canonical_history_flag_explains_a_missing_job(self):
        for payload in ({"isHistory": False}, {"history": True}, {}):
            with self.subTest(payload=payload):
                self._message(payload=payload)
                report = diagnose_engagement(lead=self.lead)
                self.assertIs(report["latest_inbound_message"]["is_history"], False)
                self.assertEqual(report["execution"]["status"], "not_queued")
                self.assertNotIn("source_message_is_history", report["blockers"])
                self.assertIn("no_hosted_ai_job_check_live_inbound_and_lead_mapping", report["blockers"])

    def test_completed_job_is_shown_after_outgoing_reply_without_api_marker(self):
        source = self._message()
        job = self._job(source, status="completed", reason="")
        self._message(direction="outbound", payload={"shvya_ai": {"job_id": str(job.pk)}})
        report = diagnose_engagement(lead=self.lead)
        self.assertEqual(report["execution"]["status"], "completed")
        self.assertEqual(report["execution"]["source_message_id"], str(source.pk))
        self.assertEqual(source.raw_payload, {})
        response = self._response()
        self.assertContains(response, "AI processing completed")
        self.assertContains(response, "Hosted reply: completed")
        self.assertNotContains(response, "No AI attempt recorded")

    def test_skipped_job_reason_survives_absent_or_conflicting_api_marker(self):
        source = self._message(payload={"shvya_ai_execution": {
            "status": "completed", "reason": "API marker must not override Hosted job",
        }})
        self._job(source, status="skipped", reason="stage_ai_disabled")
        report = diagnose_engagement(lead=self.lead)
        self.assertEqual(report["execution"]["status"], "skipped")
        self.assertEqual(report["execution"]["reason"], "stage_ai_disabled")
        response = self._response()
        self.assertContains(response, "Reply was skipped")
        self.assertContains(response, "AI is disabled for this stage.")
        self.assertNotContains(response, "API marker must not override")

    def test_completed_without_reply_explains_no_engagement(self):
        source = self._message()
        self._job(source, status="completed", reason="no_engagement")
        response = self._response()
        self.assertContains(response, "AI processing completed")
        self.assertContains(response, "no reply was needed")

    def test_processing_job_remains_authoritative_after_generation_trace_completed(self):
        source = self._message(payload={"shvya_ai_execution": {"status": "completed"}})
        self._job(source, status="processing", reason="")
        AITrace.objects.create(
            organization=self.organization, lead=self.lead,
            whatsapp_account_id=self.account.pk, source_inbound_message_id=source.pk,
            connection_type="hosted", status="completed", reason_code="private_trace_details",
        )
        report = diagnose_engagement(lead=self.lead)
        self.assertEqual(report["execution"]["status"], "processing")
        response = self._response()
        self.assertContains(response, "Preparing a reply")
        self.assertNotContains(response, "AI processing completed")
        self.assertNotContains(response, "private_trace_details")

    def test_failed_job_keeps_private_provider_errors_out_of_report_and_page(self):
        source = self._message()
        job = self._job(source, status="failed", reason="sk_private_provider_token")
        job.error = "Authorization failed: Bearer private-token"
        job.result["delivery"] = {"reason": "private customer body and provider token"}
        job.save(update_fields=["result", "error"])
        report = diagnose_engagement(lead=self.lead)
        self.assertEqual(report["execution"]["status"], "failed")
        self.assertEqual(report["execution"]["reason"], "")
        self.assertNotIn("private", json.dumps(report))
        response = self._response()
        self.assertContains(response, "Reply could not be completed")
        self.assertNotContains(response, "private")

    def test_active_hosted_account_never_uses_another_accounts_inbound(self):
        source = self._message()
        self._job(source, status="completed", reason="no_engagement")
        other_account = WhatsAppAccount.objects.create(
            organization=self.organization, connection_type="api", phone_number_id="other-api",
            display_phone_number="+919999999999", status="connected", is_active=True,
        )
        self._message(account=other_account, payload={"shvya_ai_execution": {"status": "failed"}})
        self._message(direction="outbound", payload={"shvya_ai": {"origin": "engagement"}})
        report = diagnose_engagement(lead=self.lead)
        self.assertEqual(report["account_id"], str(self.account.pk))
        self.assertEqual(report["latest_inbound_message"]["id"], str(source.pk))
        self.assertEqual(report["execution"]["status"], "completed")
        self.assertNotIn("pipeline_whatsapp_account_mismatch", report["blockers"])

    def test_malformed_job_ownership_or_kind_is_never_exposed(self):
        source = self._message()
        job = self._job(source)
        other_org = Organization.objects.create(name="Other tenant")
        other_account = WhatsAppAccount.objects.create(
            organization=other_org, connection_type="hosted", display_phone_number="+919999999999",
        )
        other_pipeline = Pipeline.objects.create(organization=other_org, name="Other sales")
        other_lead = Lead.objects.create(
            organization=other_org, pipeline=other_pipeline,
            stage=other_pipeline.stages.get(name="New leads"), name="Private customer",
            phone="+919222222222",
        )
        for changes in (
            {"organization_id": other_org.pk}, {"account_id": other_account.pk},
            {"lead_id": other_lead.pk}, {"kind": HostedAutomationJob.Kind.WELCOME},
        ):
            with self.subTest(changes=changes):
                HostedAutomationJob.objects.filter(pk=job.pk).update(**changes)
                report = diagnose_engagement(lead=self.lead)
                self.assertNotIn("hosted_job", report)
                self.assertEqual(report["execution"]["status"], "not_queued")
                HostedAutomationJob.objects.filter(pk=job.pk).update(
                    organization=self.organization, account=self.account, lead=self.lead,
                    kind=HostedAutomationJob.Kind.AI_ENGAGEMENT,
                )

    def test_status_route_rejects_another_organization(self):
        source = self._message()
        self._job(source)
        other_org = Organization.objects.create(name="Other tenant")
        with self.assertRaises(Http404):
            self._response(organization=other_org)

    def test_diagnosis_does_not_write_or_dispatch_work(self):
        source = self._message()
        job = self._job(source, status="skipped", reason="lead_ai_disabled")
        before = {"source": dict(source.raw_payload), "result": dict(job.result), "attempts": job.attempts}
        with (
            patch("apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task.apply_async") as hosted,
            patch("apps.channels.tasks.send_whatsapp_message_task.delay") as send,
            CaptureQueriesContext(connection) as queries,
        ):
            diagnose_engagement(lead=self.lead)
        hosted.assert_not_called()
        send.assert_not_called()
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        source.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(source.raw_payload, before["source"])
        self.assertEqual(job.result, before["result"])
        self.assertEqual(job.attempts, before["attempts"])

    def test_api_status_continues_to_use_its_durable_execution_marker(self):
        self.account.connection_type = "api"
        self.account.phone_number_id = "api-diagnostic"
        self.account.save(update_fields=["connection_type", "phone_number_id"])
        self._message(payload={"shvya_ai_execution": {
            "status": "failed", "reason": "engagement_generation_failed", "attempts": 2,
        }})
        report = diagnose_engagement(lead=self.lead)
        self.assertEqual(report["execution"]["status"], "failed")
        self.assertEqual(report["execution"]["reason"], "engagement_generation_failed")
        self.assertNotIn("hosted_job", report)
