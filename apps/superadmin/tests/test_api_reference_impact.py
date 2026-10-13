"""Incident cohort counts must remain scoped and label recorded references accurately."""
from django.test import override_settings

from apps.core.api_activity.incident_groups import incident_cohorts
from apps.core.api_activity.operations import update_incident
from apps.core.models import APIIncident
from apps.superadmin.tests.test_api_operations import APIActivityWorkspaceTests


@override_settings(API_ACTIVITY_ENABLED=False)
class IncidentReferenceImpactTests(APIActivityWorkspaceTests):
    def test_counts_distinct_entity_ids_without_inventing_business_impact(self):
        first = self.activity(status="failed", entity_type="message", entity_id="m-1", error_code="429")
        second = self.activity(status="failed", entity_type="message", entity_id="m-1", error_code="429")
        missing = self.activity(status="failed", entity_type="message", entity_id="", error_code="429")
        foreign = self.activity(organization=self.other_organization, status="failed",
                                entity_type="message", entity_id="m-2", error_code="429")
        for item in (first, second, missing, foreign):
            update_incident(item.pk, self.superadmin, {"action": "open", "version": "0"})
        rows = incident_cohorts(APIIncident.objects.filter(activity__organization=self.organization))
        group = next(x for x in rows if x["activity__error_code"] == "429")
        self.assertEqual(group["affected_activities"], 3)
        self.assertEqual(group["distinct_recorded_entity_ids"], 1)
        self.assertEqual(group["activities_without_entity_id"], 1)
