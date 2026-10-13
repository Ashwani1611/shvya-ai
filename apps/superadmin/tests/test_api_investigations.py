from datetime import timedelta
import uuid

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.api_activity.operations import (
    InvestigationError, StaleInvestigation, journey_report, saved_outcome, update_incident,
)
from apps.core.api_activity.retention import prune_activities
from apps.core.models import APIActivity, APIIncident, APIIncidentEvent
from apps.integrations.models import WebhookConfiguration, WebhookDelivery
from apps.superadmin.tests import test_api_operations as fixtures


@override_settings(API_ACTIVITY_ENABLED=False, CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "api-investigation-tests",
}})
class APIInvestigationTests(TestCase):
    setUp = fixtures.APIActivityWorkspaceTests.setUp
    activity = fixtures.APIActivityWorkspaceTests.activity

    def test_journey_matches_explicit_links_without_crossing_organizations(self):
        anchor = self.activity(status="failed", entity_type="message", entity_id="message-one")
        child = self.activity(parent_id=anchor.pk, correlation_id=anchor.correlation_id, operation="worker.execute")
        callback = self.activity(entity_type="message", entity_id="message-one", operation="message.state")
        self.activity(organization=self.other_organization, correlation_id=anchor.correlation_id)
        self.activity(organization=self.other_organization, parent_id=anchor.pk)
        report = journey_report(anchor)
        self.assertEqual({row["id"] for row in report["results"]}, {str(anchor.pk), str(child.pk), str(callback.pk)})
        self.assertEqual(next(row for row in report["results"] if row["id"] == str(child.pk))["link_basis"], "Recorded parent link")
        self.assertEqual(next(row for row in report["results"] if row["id"] == str(callback.pk))["link_basis"], "Shared domain reference")
        unattributed = self.activity(organization=None, organization_reference="reported-one")
        self.activity(organization=None, organization_reference="reported-two", correlation_id=unattributed.correlation_id)
        self.assertEqual(len(journey_report(unattributed)["results"]), 1)

    def test_investigation_versions_owner_validation_and_audit_preserve_outcome(self):
        activity = self.activity(status="unknown")
        incident = update_incident(activity.pk, self.superadmin, {"action": "open", "version": "0"})
        self.assertEqual(incident.version, 1)
        update_incident(activity.pk, self.superadmin, {"action": "claim", "version": "1"})
        with self.assertRaises(StaleInvestigation):
            update_incident(activity.pk, self.superadmin, {"action": "acknowledge", "version": "1"})
        with self.assertRaises(InvestigationError):
            update_incident(activity.pk, self.superadmin, {"action": "resolve", "version": "2"})
        update_incident(activity.pk, self.superadmin, {"action": "resolve", "version": "2", "note": "Provider history reviewed; Bearer sensitive-secret"})
        activity.refresh_from_db()
        self.assertEqual(activity.status, "unknown")
        incident.refresh_from_db()
        self.assertEqual(incident.status, "resolved")
        self.assertEqual(APIIncidentEvent.objects.filter(incident=incident).count(), 3)
        self.assertNotIn("sensitive-secret", str(list(incident.events.values("note", "metadata"))))
        ordinary = User.objects.create_user(email="incident-agent@example.com", password="Password123!", organization=self.organization)
        with self.assertRaises(InvestigationError):
            update_incident(activity.pk, ordinary, {"action": "reopen", "version": "3", "note": "Check again"})
        with self.assertRaises(InvestigationError):
            update_incident(activity.pk, self.superadmin, {"action": "assign", "version": "3", "owner": str(ordinary.pk)})

    def test_saved_outcome_checks_organization_and_connection_without_rewriting_activity(self):
        config = WebhookConfiguration.objects.create(organization=self.organization)
        delivery = WebhookDelivery.objects.create(webhook=config, organization=self.organization,
            lead_id=uuid.uuid4(), event_type="create", status="sent")
        activity = self.activity(integration="webhooks", entity_type="webhook", entity_id=str(delivery.pk),
                                 connection_id=str(config.pk), status="unknown")
        result = saved_outcome(activity)
        self.assertEqual(result["status"], "sent")
        self.assertIn("downstream processing is not confirmed", result["explanation"])
        update_incident(activity.pk, self.superadmin, {"action": "check", "version": "0"})
        activity.refresh_from_db()
        self.assertEqual(activity.status, "unknown")
        self.assertTrue(APIIncidentEvent.objects.get(incident__activity=activity).metadata["saved_outcome"]["supported"])
        foreign = self.activity(organization=self.other_organization, integration="webhooks",
            entity_type="webhook", entity_id=str(delivery.pk), connection_id=str(config.pk))
        self.assertFalse(saved_outcome(foreign)["supported"])
        activity.connection_id = str(uuid.uuid4())
        self.assertFalse(saved_outcome(activity)["supported"])

    def test_open_investigations_pin_evidence_until_resolved(self):
        activity = self.activity(status="failed")
        APIActivity.objects.filter(pk=activity.pk).update(created_at=timezone.now() - timedelta(days=40))
        update_incident(activity.pk, self.superadmin, {"action": "open", "version": "0"})
        self.assertEqual(prune_activities(days=30)["deleted"], 0)
        update_incident(activity.pk, self.superadmin, {"action": "resolve", "version": "1", "note": "Review complete"})
        self.assertEqual(prune_activities(days=30)["deleted"], 1)

    def test_http_controls_require_post_csrf_and_show_journey_and_inbox(self):
        activity = self.activity(status="failed")
        url = reverse("superadmin-api-incident-update", kwargs={"activity_id": activity.pk})
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies = self.client.cookies
        self.assertEqual(csrf_client.post(url, {"action": "open", "version": "0"}).status_code, 403)
        self.assertFalse(APIIncident.objects.exists())
        self.assertEqual(self.client.post(url, {"action": "open", "version": "0"}).status_code, 302)
        self.assertEqual(self.client.post(url, {"action": "claim", "version": "0"}).status_code, 409)
        page = self.client.get(reverse("superadmin-api-activity", kwargs={"activity_id": activity.pk}))
        self.assertContains(page, "Recorded journey")
        self.assertContains(page, "Check the saved outcome")
        self.assertContains(page, "Investigation audit history")
        inbox = self.client.get(reverse("superadmin-api-incidents"))
        self.assertContains(inbox, "API Client One")
        self.assertContains(inbox, "Unassigned")
        self.assertEqual(Client().post(url, {"action": "open", "version": "0"}).status_code, 302)
