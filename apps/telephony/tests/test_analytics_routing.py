from datetime import timedelta
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from apps.crm.models import Lead, LeadCall, Pipeline
from apps.telephony.analytics import format_duration
from apps.telephony.models import CallDisposition, CallRecord
from apps.telephony.services import capture_manual_crm_call, get_call_dispositions
from . import test_mobile_workspace as workspace_fixtures


class DurationTests(SimpleTestCase):
    def test_duration_units_and_boundaries(self):
        for seconds, expected in (
            (0, "0 sec"),
            (43, "43 sec"),
            (60, "1 min"),
            (102, "1 min 42 sec"),
            (3600, "1 hr"),
            (3725, "1 hr 2 min 5 sec"),
            (86405, "24 hr 5 sec"),
            (None, "—"),
            (-1, "0 sec"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)


class AnalyticsRoutingTests(TestCase):
    setUp = workspace_fixtures.MobileWorkspaceTests.setUp
    payload = workspace_fixtures.MobileWorkspaceTests.payload
    call = workspace_fixtures.MobileWorkspaceTests.call
    api = workspace_fixtures.MobileWorkspaceTests.api
    web = workspace_fixtures.MobileWorkspaceTests.web

    def test_unknown_apk_lead_routes_to_employee_instead_of_org_default(self):
        employee_pipeline = Pipeline.objects.create(
            organization=self.org, name="Employee", owner=self.user
        )
        self.pipeline.owner = None
        self.pipeline.save(update_fields=["owner"])
        call = self.call()
        self.assertEqual(call.lead.pipeline_id, employee_pipeline.id)
        self.assertEqual(call.crm_call.lead_id, call.lead_id)

    def test_existing_lead_in_peer_pipeline_retains_pipeline_and_marks_call(self):
        peer_pipeline = Pipeline.objects.create(organization=self.org, name="Peer")
        stage = peer_pipeline.stages.first()
        lead = Lead.objects.create(
            organization=self.org,
            pipeline=peer_pipeline,
            stage=stage,
            name="Existing",
            phone="+919876543210",
        )
        call = self.call()
        lead.refresh_from_db()
        self.assertEqual(lead.pipeline_id, peer_pipeline.id)
        self.assertEqual(call.lead_id, lead.id)
        self.assertEqual(call.crm_call.lead_id, lead.id)
        self.assertEqual(call.crm_call.user_id, self.user.id)

    def test_unassigned_employee_does_not_create_in_organization_default(self):
        self.pipeline.owner = None
        self.pipeline.save(update_fields=["owner"])
        with self.assertRaisesMessage(ValidationError, "assign an active pipeline"):
            self.call()
        self.assertEqual(CallRecord.objects.count(), 0)
        self.assertEqual(Lead.objects.count(), 0)
        response = self.api().post(
            "/api/v1/call-intelligence/leads/",
            {"name": "New", "phone": "9876543210"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_legacy_mobile_creation_uses_employee_pipeline(self):
        response = self.api().post(
            "/api/v1/call-intelligence/leads/",
            {"name": "New", "phone": "9876543210"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            Lead.objects.get(pk=response.data["lead_id"]).pipeline_id, self.pipeline.id
        )

    def test_filtered_metrics_exclude_unanswered_calls_from_talk_average(self):
        self.call(talk_duration_seconds=102)
        self.call(status="missed", talk_duration_seconds=0)
        response = self.web().get("/dashboard/call-intelligence/?section=analytics")
        stats = response.context["stats"]
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["average_talk"], 102)
        self.assertEqual(stats["answer_rate"], 50)
        self.assertEqual(stats["total_talk"], 102)
        self.assertContains(response, "1 min 42 sec")
        self.assertContains(response, "Call outcome")
        api = self.api().get("/api/v1/call-intelligence/analytics/?status=answered")
        self.assertEqual(api.data["stats"]["total"], 1)
        self.assertEqual(api.data["stats"]["average_talk"], 102)
        self.assertEqual(api.data["agents"][0]["answer_rate"], 100)

    def test_due_followups_exclude_overdue_and_outcome_filter_matches_metrics(self):
        call = self.call()
        CallRecord.objects.filter(pk=call.pk).update(
            disposition="converted",
            follow_up_required=True,
            follow_up_at=timezone.now() - timedelta(hours=1),
        )
        self.call()
        response = self.web().get(
            "/dashboard/call-intelligence/?section=analytics&disposition=converted"
        )
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertEqual(response.context["stats"]["overdue"], 1)
        self.assertEqual(response.context["stats"]["followups"], 0)
        self.assertEqual(response.context["agent_stats"][0]["converted"], 1)

    def test_manual_crm_capture_is_linked_and_idempotent(self):
        lead = self.call().lead
        manual = LeadCall.objects.create(
            lead=lead,
            user=self.user,
            status="completed",
            duration_seconds=43,
            called_at=timezone.now(),
        )
        first = capture_manual_crm_call(manual)
        second = capture_manual_crm_call(manual)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.source, "manual")
        self.assertEqual(first.lead_id, lead.id)
        response = self.web().get(
            "/dashboard/call-intelligence/?section=analytics&source=manual"
        )
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertContains(response, "43 sec")

    def test_standard_dispositions_backfill_without_overwriting_custom_choices(self):
        CallDisposition.objects.create(
            organization=self.org,
            code="interested",
            name="Custom interest",
            category="connected",
            is_active=False,
        )
        rows = get_call_dispositions(self.org)
        self.assertTrue(rows.filter(code="converted").exists())
        self.assertEqual(rows.get(code="call_back_later").category, "connected")
        self.assertFalse(rows.filter(code="interested").exists())
        self.assertEqual(
            CallDisposition.objects.get(organization=self.org, code="interested").name,
            "Custom interest",
        )

    def test_manual_mark_call_view_feeds_call_intelligence(self):
        from django.urls import reverse

        lead = self.call().lead
        response = self.web().post(
            reverse("crm-lead-call-save", args=[lead.id]),
            {
                "call_name": "Follow-up",
                "status": "completed",
                "duration_seconds": "2",
                "notes": "Customer requested details",
            },
        )
        self.assertEqual(response.status_code, 200)
        manual = CallRecord.objects.get(source="manual")
        self.assertEqual(manual.lead_id, lead.id)
        self.assertEqual(manual.crm_call.duration_seconds, 120)
        self.assertEqual(manual.talk_duration_seconds, 120)
        self.assertEqual(manual.user_id, self.user.id)

    def test_cloud_normalization_retains_configured_pipeline_country_code(self):
        self.pipeline.owner = None
        self.pipeline.save(update_fields=["owner"])
        Pipeline.objects.create(
            organization=self.org,
            name="US employee",
            owner=self.user,
            country_code="+1",
        )
        call = self.call(source="cloud", device_id="")
        self.assertEqual(call.phone_number, "+919876543210")
        self.assertEqual(call.lead.pipeline_id, self.pipeline.id)
