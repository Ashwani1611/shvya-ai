from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.context_processors import sidebar_nav
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization


class CallIntelligenceNavigationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Call Intelligence UI")
        self.admin = User.objects.create_user(
            email="call-admin@example.com",
            password="test-pass",
            organization=self.org,
            name="Call Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Leads",
            is_active=True,
        )
        Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            display_order=0,
            is_active=True,
        )

    def test_sidebar_exposes_call_intelligence(self):
        request = RequestFactory().get("/dashboard/call-intelligence/")
        request.crm_user = self.admin
        context = sidebar_nav(request)
        matches = [
            item
            for item in context["nav_items"]
            if item["label"] == "CALL INTELLIGENCE"
        ]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0]["is_active"])
        self.assertEqual(
            matches[0]["href"],
            reverse("call-intelligence-dashboard"),
        )

    def test_download_route_is_named(self):
        self.assertEqual(
            reverse("call-intelligence-download"),
            "/dashboard/call-intelligence/download/",
        )
