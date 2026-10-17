from datetime import datetime, timezone as dt_timezone

from django.contrib.sessions.backends.db import SessionStore
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from apps.superadmin.organization_analytics import dashboard_window, organization_analytics


class DashboardWindowTests(SimpleTestCase):
    def test_reversed_and_oversized_dates_fall_back(self):
        for params in ({"from": "bad"}, {"from": "2026-10-20", "to": "2026-10-01"}, {"from": "2020-01-01", "to": "2026-01-01"}):
            self.assertTrue(dashboard_window(params, "Asia/Kolkata")["notice"])

    def test_hourly_limits_and_timezone(self):
        result = dashboard_window({"from": "2026-10-01", "to": "2026-10-02", "granularity": "hour"}, "Asia/Kolkata")
        self.assertEqual(result["start"].utcoffset().total_seconds(), 19800)
        self.assertEqual(result["granularity"], "hour")
        self.assertEqual(dashboard_window({"from": "2026-10-01", "to": "2026-10-15", "granularity": "hour"}, "bad")["granularity"], "day")


class OrganizationAnalyticsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Console Org", timezone="Asia/Kolkata")
        self.other = Organization.objects.create(name="Other Org")
        for org in (self.org, self.other):
            pipeline = Pipeline.objects.create(organization=org, name="Sales")
            stage = Stage.objects.create(pipeline=pipeline, name="New Lead")
            lead = Lead.objects.create(organization=org, pipeline=pipeline, stage=stage, name=org.name, phone="+919900001111")
            Lead.objects.filter(pk=lead.pk).update(created_at=datetime(2026, 9, 30, 19, tzinfo=dt_timezone.utc))

    def test_tenant_scope_and_local_day_boundary(self):
        result = organization_analytics(self.org, {"from": "2026-10-01", "to": "2026-10-01"})
        self.assertEqual(result["selected_leads"], 1)
        self.assertEqual(result["total_leads"], 1)
        self.assertEqual(result["charts"][0]["rows"], [{"label": "01 Oct 2026", "value": 1}])
        self.assertEqual(result["messages"]["outbound"], 0)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(organization_analytics(self.org, {"from": "2026-09-30", "to": "2026-09-30"})["selected_leads"], 0)

    def test_page_preserves_existing_controls_and_new_sections(self):
        admin = User.objects.create_superuser(email="console@example.com", password="test", name="Admin")
        session = SessionStore()
        set_authenticated_user(session, admin)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key
        response = self.client.get(reverse("superadmin-organization-detail", args=[self.org.pk]))
        self.assertEqual(response.status_code, 200)
        for text in ("Organization Users", "Pipeline Settings", "Operational Notes", "Pending follow-ups", "Visualisations", "AI setup", "organization-console.js"):
            self.assertContains(response, text)

    def test_anonymous_cannot_read_organisation_dashboard(self):
        response = self.client.get(reverse("superadmin-organization-detail", args=[self.org.pk]))
        self.assertEqual(response.status_code, 302)

    def test_delivery_counts_do_not_include_other_tenants(self):
        from apps.channels.models import WhatsAppAccount, WhatsAppMessage
        from django.utils import timezone
        for org in (self.org, self.other):
            account = WhatsAppAccount.objects.create(organization=org)
            for status in ("sent", "delivered", "failed"):
                WhatsAppMessage.objects.create(organization=org, account=account, direction="outbound", status=status)
        today = timezone.localdate().isoformat()
        report = organization_analytics(self.org, {"from": today, "to": today})
        self.assertEqual(report["messages"]["outbound"], 3)
        self.assertEqual(report["messages"]["delivered"], 1)
        self.assertEqual(report["messages"]["failed"], 1)

    def test_lookup_requires_superadmin_and_links_to_console(self):
        url = reverse("superadmin-organization-lookup")
        self.assertEqual(self.client.get(url, {"q": "Console"}).status_code, 302)
        admin = User.objects.create_superuser(email="lookup@example.com", password="test", name="Admin")
        session = SessionStore()
        set_authenticated_user(session, admin)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key
        response = self.client.get(url, {"q": "Console"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["url"], reverse("superadmin-organization-detail", args=[self.org.pk]))
        self.assertEqual(response["Cache-Control"], "private, no-store")
