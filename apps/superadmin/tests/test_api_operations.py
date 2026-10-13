from datetime import timedelta
from types import SimpleNamespace
import uuid

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.api_activity.queries import InvalidActivityFilter, parse_filters, serialize_activity
from apps.core.models import APIActivity, APIActivityEvent
from apps.organizations.models import Organization
from apps.superadmin.api_views import (
    api_activity_data_view, api_activity_view, api_operations_data_view, api_operations_view,
    api_incidents_view, api_incident_update_view,
)


class APIActivityFilterAndAccessTests(SimpleTestCase):
    def test_invalid_organization_never_falls_back_to_global_search(self):
        with self.assertRaises(InvalidActivityFilter):
            parse_filters({"organization": "not-an-organization"})

    def test_bounds_and_identifier_search_preserve_provider_ids(self):
        filters = parse_filters({"q": "123456789012345", "page": "999999", "per_page": "999999"})
        self.assertEqual(filters["q"], "123456789012345")
        self.assertEqual(filters["page"], 1000)
        self.assertEqual(filters["per_page"], 100)
        with self.assertRaises(InvalidActivityFilter):
            parse_filters({"range": "all"})

    def test_all_views_reject_non_superadmins_before_reading_data(self):
        factory = RequestFactory()
        views = ((api_operations_view, {}), (api_operations_data_view, {}),
                 (api_incidents_view, {}), (api_incident_update_view, {"activity_id": uuid.uuid4()}),
                 (api_activity_view, {"activity_id": uuid.uuid4()}),
                 (api_activity_data_view, {"activity_id": uuid.uuid4()}))
        for user in (AnonymousUser(), SimpleNamespace(is_authenticated=True, is_superuser=False)):
            for view, kwargs in views:
                request = factory.get("/superadmin/api/")
                request.user = user
                response = view(request, **kwargs)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/superadmin/login/", response.url)
                self.assertIn("no-store", response["Cache-Control"])

    def test_read_endpoints_do_not_accept_posts(self):
        request = RequestFactory().post("/superadmin/api/data/")
        request.user = SimpleNamespace(is_authenticated=True, is_superuser=True)
        self.assertEqual(api_operations_data_view(request).status_code, 405)

    def test_serializer_redacts_existing_metadata_defensively(self):
        activity = APIActivity(
            integration="whatsapp_api", operation="message.send", status="unknown",
            provider_reference="123456789012345", metadata={"access_token": "private-secret", "count": 2},
            endpoint="https://user:password@example.com/messages?access_token=private-secret",
            error_message="Authorization: Bearer sensitive-value",
        )
        serialized = serialize_activity(activity, detail=True)
        self.assertEqual(serialized["provider_reference"], "123456789012345")
        self.assertNotIn("private-secret", str(serialized))
        self.assertNotIn("sensitive-value", str(serialized))
        self.assertEqual(serialized["endpoint"], "https://example.com/messages")
        self.assertEqual(serialized["metadata"]["count"], 2)


@override_settings(API_ACTIVITY_ENABLED=False, CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "api-operations-workspace-tests",
}})
class APIActivityWorkspaceTests(TestCase):
    def setUp(self):
        # Every test logs in through the real protected endpoint. Rate-limit
        # counters belong to this fixture, not the preceding test's requests.
        cache.clear()
        self.addCleanup(cache.clear)
        self.organization = Organization.objects.create(name="API Client One")
        self.other_organization = Organization.objects.create(name="API Client Two")
        self.superadmin = User.objects.create_superuser(email="api-superadmin@example.com", password="Password123!")
        response = self.client.post(reverse("superadmin-login"), {"username": self.superadmin.email, "password": "Password123!"})
        self.assertEqual(response.status_code, 302)

    def activity(self, **fields):
        defaults = {"organization": self.organization, "integration": "whatsapp_api", "kind": "http", "operation": "message.send", "status": "accepted"}
        defaults.update(fields)
        return APIActivity.objects.create(**defaults)

    def test_dashboard_scopes_results_and_never_claims_accepted_delivery(self):
        self.activity(provider_reference="111111111111111")
        self.activity(organization=self.other_organization, status="failed", provider_reference="222222222222222")
        url = reverse("superadmin-api-data")
        response = self.client.get(url, {"organization": str(self.organization.pk)})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["waiting"], 0)
        self.assertEqual(data["results"][0]["status"], "accepted")
        self.assertNotContains(response, "222222222222222")
        self.assertIn("no-store", response["Cache-Control"])
        page = self.client.get(reverse("superadmin-api"))
        self.assertContains(page, "API Operations Center")
        self.assertContains(page, "New activity capture is disabled")
        self.assertContains(page, ">API</span>", html=False)

    def test_deadlines_mark_overdue_without_rewriting_outcome(self):
        activity = self.activity(expected_by=timezone.now() - timedelta(minutes=1))
        self.activity(status="completed", expected_by=timezone.now() - timedelta(minutes=1))
        response = self.client.get(reverse("superadmin-api-data"), {"status": "overdue"})
        data = response.json()
        self.assertEqual(data["summary"]["overdue"], 1)
        self.assertEqual(data["results"][0]["id"], str(activity.pk))
        self.assertEqual(data["results"][0]["status"], "accepted")

    def test_attention_count_and_rows_link_to_org_and_debug_evidence(self):
        failure = self.activity(status="failed", operation="message.send", error_code="131042",
                                endpoint="https://graph.facebook.com/messages", method="POST",
                                summary="Provider rejected the send")
        self.activity(organization=self.other_organization, status="blocked", operation="lead.route")
        self.activity(status="accepted", expected_by=timezone.now() - timedelta(minutes=2))
        self.activity(status="completed")
        response = self.client.get(reverse("superadmin-api"), {"range": "7d", "status": "completed"})
        self.assertContains(response, 'href="/superadmin/api/?range=7d&amp;status=attention"')
        self.assertContains(response, 'href="/superadmin/api/?range=7d&amp;status=waiting"')
        self.assertContains(response, "View all 3")
        self.assertContains(response, "API Client One")
        self.assertContains(response, "API Client Two")
        self.assertContains(response, "POST https://graph.facebook.com/messages")
        self.assertContains(response, "Error 131042")
        self.assertContains(response, f"Org {str(self.organization.pk)[:8]}", count=1)
        self.assertContains(response, f"Activity {str(failure.pk)[:8]}", count=0)

        attention = self.client.get(reverse("superadmin-api"), {"range": "7d", "status": "attention"})
        self.assertContains(attention, f"Activity {str(failure.pk)[:8]}")
        self.assertContains(attention, f"Org {str(self.organization.pk)[:8]}")
        self.assertContains(attention, "Attribution unavailable", count=0)
        self.assertContains(attention, "Overdue")
        self.assertContains(attention, "Inspect evidence")
        self.assertContains(attention, f"/superadmin/api/activities/{failure.pk}/?back=")

    def test_waiting_filter_only_includes_open_work_with_a_deadline(self):
        waiting = self.activity(status="processing", expected_by=timezone.now() + timedelta(minutes=5))
        self.activity(status="queued")
        self.activity(status="completed", expected_by=timezone.now() + timedelta(minutes=5))
        response = self.client.get(reverse("superadmin-api-data"), {"status": "waiting"})
        self.assertEqual([item["id"] for item in response.json()["results"]], [str(waiting.pk)])

    def test_detail_shows_last_recorded_step_and_safe_filtered_back_link(self):
        activity = self.activity(status="failed", organization=None,
                                 organization_reference="reported-org-id", operation="message.send")
        APIActivityEvent.objects.create(activity=activity, name="provider.rejected", status="failed",
                                        summary="Provider rejection recorded")
        detail_url = reverse("superadmin-api-activity", kwargs={"activity_id": activity.pk})
        response = self.client.get(detail_url, {"back": "/superadmin/api/?status=attention"})
        self.assertContains(response, 'href="/superadmin/api/?status=attention"')
        self.assertContains(response, "Last recorded step")
        self.assertContains(response, "provider.rejected")
        self.assertContains(response, "Reported org reference")
        self.assertContains(response, "attribution unverified")
        self.assertContains(response, "Unattributed / platform")
        unsafe = self.client.get(detail_url, {"back": "https://example.com/steal"})
        self.assertContains(unsafe, 'href="/superadmin/api/"')
        self.assertNotContains(unsafe, "example.com/steal")
        data = self.client.get(reverse("superadmin-api-activity-data", kwargs={"activity_id": activity.pk})).json()
        self.assertEqual(data["last_event"]["name"], "provider.rejected")

    def test_historical_queued_observations_do_not_count_as_current_waiting(self):
        self.activity(status="queued", kind="message", operation="message.created")
        self.activity(status="processing", kind="connection", operation="session.state_observed")
        self.activity(status="queued", kind="task", expected_by=timezone.now() + timedelta(minutes=5))
        response = self.client.get(reverse("superadmin-api-data"))
        self.assertEqual(response.json()["summary"]["waiting"], 1)

    def test_background_job_filter_uses_kind_across_provider_integrations(self):
        task = self.activity(kind="task", integration="instagram", operation="instagram.process")
        self.activity(kind="http", integration="instagram")
        response = self.client.get(reverse("superadmin-api-data"), {"integration": "celery"})
        self.assertEqual([item["id"] for item in response.json()["results"]], [str(task.pk)])

    def test_related_callbacks_use_exact_ids_with_organization_boundary(self):
        activity = self.activity(provider_reference="provider-message", entity_type="message", entity_id="local-id")
        callback = self.activity(provider_reference="provider-message", status="completed", operation="message.status")
        self.activity(provider_reference="provider-message", organization=self.other_organization, operation="foreign.message")
        APIActivityEvent.objects.create(activity=activity, name="provider.accepted", status="accepted", metadata={"authorization": "private-secret", "http_status": 200})
        response = self.client.get(reverse("superadmin-api-activity-data", kwargs={"activity_id": activity.pk}))
        data = response.json()
        self.assertEqual([item["id"] for item in data["related"]], [str(callback.pk)])
        self.assertEqual(data["events"][0]["name"], "provider.accepted")
        self.assertNotContains(response, "private-secret")
        page = self.client.get(reverse("superadmin-api-activity", kwargs={"activity_id": activity.pk}))
        self.assertContains(page, "Request accepted; final outcome unconfirmed")
        self.assertContains(page, "Activity timeline")

    def test_invalid_filter_returns_error_and_post_never_mutates(self):
        self.activity()
        response = self.client.get(reverse("superadmin-api-data"), {"organization": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_filter")
        before = APIActivity.objects.count()
        response = self.client.post(reverse("superadmin-api-data"))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(APIActivity.objects.count(), before)
