"""Regression checks for API health summaries and incident discovery."""
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.api_activity.health import discover_attention, operations_health
from apps.core.api_activity.queries import parse_filters
from apps.core.models import APIActivity, APIIncident
from apps.core.tasks import discover_api_incidents
from apps.superadmin.api_views import api_health_data_view
from apps.superadmin.tests.test_api_operations import APIActivityWorkspaceTests


@override_settings(API_ACTIVITY_ENABLED=False)
class APIHealthTests(APIActivityWorkspaceTests):
    def age_activity(self, activity, *, days, updated_days=None):
        fields = {"created_at": timezone.now() - timedelta(days=days)}
        if updated_days is not None:
            fields["updated_at"] = timezone.now() - timedelta(days=updated_days)
        APIActivity.objects.filter(pk=activity.pk).update(**fields)
        return activity

    def test_health_is_organization_scoped(self):
        self.activity(status="failed", error_code="429")
        self.activity(organization=self.other_organization, status="completed")
        report = operations_health(hours=24, organization_id=self.organization.id)
        self.assertEqual(len(report["organizations"]), 1)
        self.assertEqual(report["organizations"][0]["failures"], 1)
        self.assertEqual(report["organizations"][0]["completed"], 0)
        self.assertNotIn(str(self.other_organization.pk), str(report["issue_cohorts"]))

    def test_discovery_is_idempotent_and_does_not_rewrite_evidence(self):
        failed = self.activity(status="failed")
        self.assertEqual(discover_attention()["opened"], 1)
        self.assertEqual(discover_attention()["opened"], 0)
        self.assertEqual(APIIncident.objects.filter(activity=failed).count(), 1)
        failed.refresh_from_db()
        self.assertEqual(failed.status, "failed")

    def test_health_api_requires_superadmin_and_valid_hours(self):
        url = reverse("superadmin-api-health-data")
        self.assertEqual(self.client.get(url, {"hours": "2"}).status_code, 400)
        result = self.client.get(url, {"hours": "24"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("organizations", result.json())

    def test_ninety_days_is_not_silently_reduced_to_thirty(self):
        included = self.age_activity(self.activity(status="failed"), days=45)
        self.age_activity(self.activity(status="failed"), days=95)
        response = self.client.get(reverse("superadmin-api"), {"range": "90d"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["total"], 1)
        row = response.context["organization_health"][0]
        self.assertEqual(row["total"], 1)
        follow = self.client.get(row["attention_url"])
        self.assertEqual([item["id"] for item in follow.context["results"]], [str(included.pk)])
        for params in ({"hours": "2160"}, {"range": "90d"}):
            with self.subTest(params=params):
                data = self.client.get(reverse("superadmin-api-health-data"), params).json()
                self.assertEqual(data["window_hours"], 2160)
                self.assertEqual(data["organizations"][0]["total"], 1)

    def test_health_table_and_drilldown_keep_all_non_outcome_filters(self):
        first = self.age_activity(self.activity(status="failed", error_code="429"), days=3)
        second = self.activity(status="unknown", error_code="429")
        self.activity(status="failed", error_code="401")
        self.activity(status="failed", error_code="429", integration="instagram")
        self.activity(status="failed", error_code="429", kind="task")
        self.activity(status="failed", error_code="429", organization=self.other_organization)
        self.age_activity(self.activity(status="failed", error_code="429"), days=8)
        params = {
            "range": "7d", "organization": str(self.organization.pk),
            "integration": "whatsapp_api", "kind": "http", "q": "429",
            "status": "completed", "page": "3", "per_page": "50",
        }
        response = self.client.get(reverse("superadmin-api"), params)
        self.assertEqual(response.status_code, 200)
        row = response.context["organization_health"][0]
        self.assertEqual(row["total"], 2)
        self.assertEqual(row["attention"], response.context["summary"]["attention"])
        query = parse_qs(urlsplit(row["attention_url"]).query)
        for key in ("range", "organization", "integration", "kind", "q", "per_page"):
            self.assertEqual(query[key], [params[key]])
        self.assertEqual(query["status"], ["attention"])
        self.assertNotIn("page", query)
        follow = self.client.get(row["attention_url"])
        self.assertEqual({item["id"] for item in follow.context["results"]}, {str(first.pk), str(second.pk)})
        data = self.client.get(reverse("superadmin-api-health-data"), params).json()
        self.assertEqual(data["organizations"][0]["total"], 2)
        self.assertEqual(data["window_hours"], 168)

    def test_organisation_links_keep_search_when_switching_tenant(self):
        self.activity(status="failed", error_code="429")
        response = self.client.get(reverse("superadmin-api"), {"q": "429", "range": "7d"})
        query = parse_qs(urlsplit(response.context["results"][0]["organization_url"]).query)
        self.assertEqual(query["q"], ["429"])
        self.assertEqual(query["range"], ["7d"])
        self.assertEqual(query["organization"], [str(self.organization.pk)])

    def test_background_job_health_uses_kind_across_integrations(self):
        first = self.activity(status="failed", kind="task", integration="instagram")
        second = self.activity(status="failed", kind="task", integration="whatsapp_api")
        self.activity(status="failed", kind="http", integration="instagram")
        filters = parse_filters({"integration": "celery"})
        row = operations_health(filters=filters)["organizations"][0]
        self.assertEqual(row["total"], 2)
        response = self.client.get(row["attention_url"])
        self.assertEqual({item["id"] for item in response.context["results"]}, {str(first.pk), str(second.pk)})

    def test_unattributed_drilldown_never_broadens_to_all_tenants(self):
        target = self.activity(status="failed", organization=None)
        self.activity(status="failed")
        self.activity(status="failed", organization=self.other_organization)
        report = operations_health()
        row = next(item for item in report["organizations"] if item["organization_id"] is None)
        self.assertEqual(row["attention"], 1)
        self.assertEqual(parse_qs(urlsplit(row["attention_url"]).query)["organization"], ["unattributed"])
        response = self.client.get(row["attention_url"])
        self.assertEqual([item["id"] for item in response.context["results"]], [str(target.pk)])
        self.assertContains(response, '<option value="unattributed" selected>Unattributed / platform</option>', html=True)
        for route in ("superadmin-api-data", "superadmin-api-health-data"):
            data = self.client.get(reverse(route), {"organization": "unattributed"}).json()
            rows = data.get("results", data.get("organizations", []))
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["organization_id"])

    def test_unattributed_investigations_keep_the_same_boundary(self):
        target = self.activity(status="failed", organization=None)
        self.activity(status="failed")
        discover_attention()
        response = self.client.get(reverse("superadmin-api-incidents"), {"organization": "unattributed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["activity"]["id"] for item in response.context["results"]], [str(target.pk)])
        self.assertEqual(response.context["active_count"], 1)

    def test_attention_matches_summary_and_includes_overdue_without_calling_it_failed(self):
        now = timezone.now()
        for status in ("failed", "unknown", "blocked", "partial"):
            self.activity(status=status)
        overdue_rows = [self.activity(status=status, expected_by=now - timedelta(minutes=1))
                        for status in ("queued", "processing", "accepted")]
        self.activity(status="queued")
        self.activity(status="accepted", expected_by=now + timedelta(hours=1))
        self.activity(status="completed", expected_by=now - timedelta(hours=1))
        report = operations_health(now=now)
        row = report["organizations"][0]
        self.assertEqual(row["attention"], 7)
        self.assertEqual(row["failures"], 4)
        self.assertEqual(row["overdue"], 3)
        self.assertEqual(len(report["recent_attention"]), 7)
        response = self.client.get(reverse("superadmin-api"))
        self.assertEqual(response.context["summary"]["attention"], 7)
        self.assertEqual(response.context["organization_health"][0]["attention"], 7)
        self.assertContains(response, "Inspect attention")
        for activity in overdue_rows:
            activity.refresh_from_db()
            self.assertIn(activity.status, ("queued", "processing", "accepted"))

    def test_health_one_hour_link_does_not_expand_to_twenty_four_hours(self):
        included = self.activity(status="failed")
        self.age_activity(self.activity(status="failed"), days=2 / 24)
        data = self.client.get(reverse("superadmin-api-health-data"), {"hours": "1"}).json()
        self.assertEqual(data["window_hours"], 1)
        row = data["organizations"][0]
        self.assertEqual(row["total"], 1)
        response = self.client.get(row["attention_url"])
        self.assertEqual(response.context["filters"]["range"], "1h")
        self.assertEqual([item["id"] for item in response.context["results"]], [str(included.pk)])

    def test_default_discovery_recovers_old_unreviewed_failure_backlog(self):
        failed = self.age_activity(self.activity(status="failed"), days=10, updated_days=10)
        self.assertEqual(discover_attention()["opened"], 1)
        self.assertTrue(APIIncident.objects.filter(activity=failed).exists())
        self.assertEqual(discover_attention()["opened"], 0)

    def test_old_overdue_activity_is_found_without_a_recent_write(self):
        overdue = self.age_activity(self.activity(
            status="processing", expected_by=timezone.now() - timedelta(minutes=1),
        ), days=10, updated_days=10)
        self.assertEqual(discover_attention(hours=24)["opened"], 1)
        self.assertTrue(APIIncident.objects.filter(activity=overdue).exists())
        overdue.refresh_from_db()
        self.assertEqual(overdue.status, "processing")

    def test_manual_lookback_uses_updated_time_not_creation_time(self):
        newly_failed = self.age_activity(self.activity(status="failed"), days=10)
        stale = self.age_activity(self.activity(status="failed"), days=10, updated_days=10)
        self.assertEqual(discover_attention(hours=24)["opened"], 1)
        self.assertTrue(APIIncident.objects.filter(activity=newly_failed).exists())
        self.assertFalse(APIIncident.objects.filter(activity=stale).exists())
        self.assertEqual(discover_attention()["opened"], 1)

    def test_bounded_scans_progress_without_reopening_resolved_reviews(self):
        resolved = self.age_activity(self.activity(status="failed"), days=20)
        incident = APIIncident.objects.create(activity=resolved, status="resolved", due_at=timezone.now())
        pending = [self.age_activity(self.activity(status="failed"), days=days, updated_days=days)
                   for days in (10, 8, 6)]
        for index, target in enumerate(pending, start=1):
            result = discover_attention(limit=1)
            self.assertEqual(result["opened"], 1)
            self.assertEqual(result["scanned_max"], 1)
            self.assertEqual(APIIncident.objects.filter(status="open").count(), index)
            self.assertTrue(APIIncident.objects.filter(activity=target).exists())
        self.assertEqual(discover_attention(limit=1)["opened"], 0)
        incident.refresh_from_db()
        self.assertEqual(incident.status, "resolved")

    def test_scheduled_discovery_has_no_age_cutoff_and_obeys_capture_switch(self):
        target = self.age_activity(self.activity(status="failed"), days=10, updated_days=10)
        self.assertEqual(discover_api_incidents.run(), {"skipped": "capture disabled"})
        self.assertFalse(APIIncident.objects.exists())
        with override_settings(API_ACTIVITY_ENABLED=True):
            result = discover_api_incidents.run()
        self.assertEqual(result["opened"], 1)
        self.assertEqual(result["scanned_max"], 200)
        self.assertIsNone(result["window_hours"])
        self.assertTrue(APIIncident.objects.filter(activity=target).exists())

    def test_manual_command_defaults_to_retained_backlog(self):
        target = self.age_activity(self.activity(status="failed"), days=10, updated_days=10)
        call_command("discover_api_incidents", limit=1, stdout=StringIO())
        self.assertTrue(APIIncident.objects.filter(activity=target).exists())

    def test_discovery_rejects_invalid_bounds(self):
        for kwargs in ({"hours": 0}, {"hours": 721}, {"limit": 0}, {"limit": 501}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                discover_attention(**kwargs)

    def test_invalid_health_filters_do_not_fall_back_to_a_global_report(self):
        for params in (
            {"organization": "not-a-tenant"}, {"organization": "null"},
            {"hours": "2160", "range": "30d"}, {"hours": "bad"},
            {"range": "all"}, {"kind": "invalid!"}, {"integration": "invalid!"},
        ):
            with self.subTest(params=params):
                response = self.client.get(reverse("superadmin-api-health-data"), params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_filter")

    def test_health_access_is_rejected_before_any_data_query(self):
        for user in (AnonymousUser(), SimpleNamespace(is_authenticated=True, is_superuser=False)):
            request = RequestFactory().get(reverse("superadmin-api-health-data"))
            request.user = user
            with self.assertNumQueries(0):
                response = api_health_data_view(request)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/superadmin/login/", response.url)
            self.assertIn("no-store", response["Cache-Control"])

    def test_health_reads_do_not_create_reviews_or_change_outcomes(self):
        activity = self.activity(status="unknown")
        for route in ("superadmin-api", "superadmin-api-health-data", "superadmin-api-data"):
            self.assertEqual(self.client.get(reverse(route)).status_code, 200)
        self.assertFalse(APIIncident.objects.exists())
        activity.refresh_from_db()
        self.assertEqual(activity.status, "unknown")
