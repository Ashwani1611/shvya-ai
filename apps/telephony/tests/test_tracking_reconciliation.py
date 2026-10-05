from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.crm.models import Lead, LeadCall, Pipeline
from apps.telephony.models import CallIntelligenceSettings, CallRecord
from apps.telephony.services import normalize_call_phone, reconcile_lead_calls
from apps.telephony.tasks import recover_call_intelligence

from . import test_mobile_workspace as fixtures


class TrackingReconciliationTests(TestCase):
    setUp = fixtures.MobileWorkspaceTests.setUp
    payload = fixtures.MobileWorkspaceTests.payload
    call = fixtures.MobileWorkspaceTests.call
    api = fixtures.MobileWorkspaceTests.api
    web = fixtures.MobileWorkspaceTests.web

    def manual_lead(self, *, organization=None, pipeline=None, phone="+919876543210"):
        pipeline = pipeline or self.pipeline
        return Lead.objects.create(
            organization=organization or self.org, pipeline=pipeline,
            stage=pipeline.stages.first(), name="Manual CRM prospect", phone=phone,
        )

    def test_lead_created_after_call_links_existing_call_and_crm_history_once(self):
        CallIntelligenceSettings.objects.filter(organization=self.org).update(enabled=False)
        call = self.call()
        self.assertIsNone(call.lead_id)
        peer = Pipeline.objects.create(organization=self.org, name="Other team pipeline")
        with self.captureOnCommitCallbacks(execute=True):
            lead = self.manual_lead(pipeline=peer)
        reconcile_lead_calls(lead.id)
        call.refresh_from_db()
        self.assertEqual(call.lead_id, lead.id)
        self.assertEqual(call.lead.pipeline_id, peer.id)
        self.assertEqual(call.crm_call.user_id, self.user.id)
        self.assertEqual(call.crm_call.duration_seconds, 80)
        self.assertEqual(lead.calls.count(), 1)
        response = self.web().get("/dashboard/call-intelligence/?section=analytics")
        self.assertNotContains(response, "Not in CRM")
        self.assertContains(response, lead.name)
        self.assertContains(response, peer.name)

    def test_existing_manual_lead_matches_dialer_trunk_and_international_prefixes(self):
        lead = self.manual_lead()
        for number in ["09876543210", "0091 9876543210", "+91-98765-43210"]:
            with self.subTest(number=number):
                call = self.call(phone_number=number, raw_phone_number=number)
                self.assertEqual(call.lead_id, lead.id)
                self.assertEqual(call.phone_number, lead.phone)
                self.assertEqual(call.crm_call.lead_id, lead.id)
        self.assertEqual(lead.calls.count(), 3)

    def test_dashboard_repairs_legacy_links_before_pipeline_filter_and_metrics(self):
        lead = self.manual_lead()
        old = CallRecord.objects.create(
            organization=self.org, user=self.user, source="cloud", source_call_id="old",
            phone_number=lead.phone, contact_name="Old phone contact",
            direction="outgoing", status="answered", ended_at=timezone.now(),
            talk_duration_seconds=43,
        )
        response = self.web().get(
            f"/dashboard/call-intelligence/?section=analytics&pipeline={self.pipeline.id}"
        )
        old.refresh_from_db()
        self.assertEqual(old.lead_id, lead.id)
        self.assertEqual(old.crm_call.duration_seconds, 43)
        self.assertEqual(response.context["stats"]["linked"], 1)
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertContains(response, "Manual CRM prospect")
        self.assertNotContains(response, "Not in CRM")
        api = self.api().get(f"/api/v1/call-intelligence/calls/?pipeline_id={self.pipeline.id}")
        self.assertEqual(api.data["calls"][0]["lead"]["id"], str(lead.id))
        self.assertEqual(lead.calls.count(), 1)

    def test_linking_never_matches_same_phone_in_another_organization(self):
        CallIntelligenceSettings.objects.filter(organization=self.org).update(enabled=False)
        call = self.call()
        other_pipeline = Pipeline.objects.get(organization=self.other_org, name="Leads")
        with self.captureOnCommitCallbacks(execute=True):
            self.manual_lead(organization=self.other_org, pipeline=other_pipeline)
        self.web().get("/dashboard/call-intelligence/?section=analytics")
        call.refresh_from_db()
        self.assertIsNone(call.lead_id)
        self.assertIsNone(call.crm_call_id)

    def test_early_call_is_linked_but_only_terminal_event_creates_crm_call(self):
        CallIntelligenceSettings.objects.filter(organization=self.org).update(enabled=False)
        source_id = "in-progress-call"
        call = self.call(source_call_id=source_id, event_type="ringing", ended_at=None)
        with self.captureOnCommitCallbacks(execute=True):
            lead = self.manual_lead()
        call.refresh_from_db()
        self.assertEqual(call.lead_id, lead.id)
        self.assertIsNone(call.crm_call_id)
        self.call(source_call_id=source_id)
        self.call(source_call_id=source_id)
        self.assertEqual(lead.calls.count(), 1)

    @patch("apps.telephony.tasks.analyze_call_intelligence.delay")
    def test_legacy_manual_calls_are_backfilled_and_notes_are_queued(self, publish):
        lead = self.manual_lead()
        crm_call = LeadCall.objects.create(
            lead=lead, user=self.user, status="completed", called_at=timezone.now(),
            duration_seconds=102, notes="Requested a demo next Tuesday.",
        )
        with self.captureOnCommitCallbacks(execute=True):
            recover_call_intelligence.run()
            recover_call_intelligence.run()
        record = CallRecord.objects.get(crm_call=crm_call)
        self.assertEqual(record.source, "manual")
        self.assertEqual(record.talk_duration_seconds, 102)
        self.assertEqual(record.lead_id, lead.id)
        self.assertEqual(record.analysis_status, "queued")
        publish.assert_called_once_with(str(record.id), record.analysis_input_hash)

    def test_international_prefix_uses_explicit_country_without_adding_pipeline_country(self):
        self.assertEqual(normalize_call_phone("0044 7700 900123", pipeline=self.pipeline), "+447700900123")

    def test_late_ringing_event_preserves_completed_status_and_crm_duration(self):
        call = self.call(source_call_id="late-ringing")
        ended_at = call.ended_at
        self.call(
            source_call_id="late-ringing", event_type="ringing", status="unknown",
            ended_at=None, talk_duration_seconds=0,
        )
        call.refresh_from_db()
        self.assertEqual(call.status, "answered")
        self.assertEqual(call.ended_at, ended_at)
        self.assertEqual(call.talk_duration_seconds, 80)
        self.assertEqual(call.crm_call.status, "completed")
        self.assertEqual(call.crm_call.duration_seconds, 80)
