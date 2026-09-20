from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class CrossPipelineLeadNavigationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Cross pipeline navigation")
        self.user = User.objects.create_user(
            email="cross-pipeline@example.com",
            password="test-password",
            name="Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.default_pipeline = self.organization.pipelines.get(name="Leads")
        self.target_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
        )
        self.target_stage = self.target_pipeline.stages.order_by("display_order")[1]
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.target_pipeline,
            stage=self.target_stage,
            name="Cross Pipeline Result",
            phone="+919876500001",
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def test_all_pipeline_result_links_to_exact_pipeline_stage_and_lead(self):
        response = self.client.get(
            reverse("crm-lead-table-partial"),
            {
                "pipeline": self.default_pipeline.pk,
                "filter_pipeline": "all",
                "search": "Cross Pipeline Result",
            },
        )

        self.assertEqual(response.status_code, 200)
        expected_url = (
            reverse("crm-dashboard")
            + f"?pipeline={self.target_pipeline.pk}"
            + f"&stage={self.target_stage.pk}"
            + f"&lead={self.lead.pk}"
        )
        self.assertContains(response, expected_url)

    def test_dashboard_deep_link_loads_requested_stage_before_opening_lead(self):
        response = self.client.get(
            reverse("crm-dashboard"),
            {
                "pipeline": self.target_pipeline.pk,
                "stage": self.target_stage.pk,
                "lead": self.lead.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        expected_partial = (
            reverse("crm-lead-table-partial")
            + f"?pipeline={self.target_pipeline.pk}"
            + f"&stage={self.target_stage.pk}"
        )
        self.assertContains(response, expected_partial)
        self.assertContains(response, str(self.lead.pk))
