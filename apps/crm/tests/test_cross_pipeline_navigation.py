from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escapejs

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
        self.assertContains(
            response,
            f'const requestedLeadId = "{escapejs(str(self.lead.pk))}";',
        )

    def test_stage_pages_keep_every_lead_reachable(self):
        for index in range(42):
            Lead.objects.create(
                organization=self.organization,
                pipeline=self.target_pipeline,
                stage=self.target_stage,
                name=f"Paged lead {index}",
                phone=f"+9198766{index:05d}",
            )

        url = reverse("crm-lead-table-partial")
        first = self.client.get(url, {
            "pipeline": self.target_pipeline.pk,
            "stage": self.target_stage.pk,
        })
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "Page 1 of 2")
        self.assertContains(first, "Showing 1–40 of 43 leads")
        self.assertContains(first, "Next")
        self.assertNotContains(first, f'id="lead-card-{self.lead.pk}"')

        second = self.client.get(url, {
            "pipeline": self.target_pipeline.pk,
            "stage": self.target_stage.pk,
            "page": 2,
        })
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, f'id="lead-card-{self.lead.pk}"')

        deep_link = self.client.get(url, {
            "pipeline": self.target_pipeline.pk,
            "stage": self.target_stage.pk,
            "lead": self.lead.pk,
        })
        self.assertEqual(deep_link.status_code, 200)
        self.assertContains(deep_link, f'id="lead-card-{self.lead.pk}"')

    def test_stage_count_endpoint_reports_database_totals_across_pages(self):
        route = reverse("crm-lead-stage-counts")
        response = self.client.get(route, {"pipeline": self.target_pipeline.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["counts"][str(self.target_stage.pk)], 1)
        for index in range(2):
            Lead.objects.create(
                organization=self.organization,
                pipeline=self.target_pipeline,
                stage=self.target_stage,
                name=f"Counted lead {index}",
                phone=f"+91987770000{index}",
            )
        response = self.client.get(route, {"pipeline": self.target_pipeline.pk})
        self.assertEqual(response.json()["counts"][str(self.target_stage.pk)], 3)
        self.lead.stage = self.target_pipeline.stages.order_by("display_order").first()
        self.lead.save(update_fields=["stage"])
        response = self.client.get(route, {"pipeline": self.target_pipeline.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["counts"][str(self.target_stage.pk)], 2)
        self.assertEqual(self.client.get(route, {"pipeline": "invalid"}).status_code, 404)
