"""Regression checks for API health summaries and incident discovery."""
from django.test import override_settings
from django.urls import reverse

from apps.core.api_activity.health import discover_attention, operations_health
from apps.core.models import APIIncident
from apps.superadmin.tests.test_api_operations import APIActivityWorkspaceTests


@override_settings(API_ACTIVITY_ENABLED=False)
class APIHealthTests(APIActivityWorkspaceTests):
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
