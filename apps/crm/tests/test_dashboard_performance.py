from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import (
    AttributeDefinition,
    Lead,
    LeadCall,
    LeadNote,
    LeadReminder,
)
from apps.organizations.models import Organization
from services.crm.attribute_cache import get_cached_attribute_definitions
from services.crm.dashboard_query_service import build_lead_table_context


LOC_MEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}


@override_settings(CACHES=LOC_MEM_CACHE)
class LeadDashboardPerformanceTests(TestCase):

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Dashboard performance org",
        )
        self.user = User.objects.create_user(
            email="dashboard-perf@example.com",
            organization=self.organization,
            password="test-password",
            name="Dashboard Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline = self.organization.pipelines.get(
            name="Leads"
        )
        self.stage = self.pipeline.stages.order_by(
            "display_order"
        ).first()
        self.request = RequestFactory().get(
            "/dashboard/leads/table/"
        )

        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Company",
            key="company",
            display_order=0,
        )

    def _create_lead(self, index):
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name=f"Lead {index}",
            phone=f"+91987654{index:04d}",
            email=f"lead{index}@example.com",
            notes=f"Note {index}",
            attributes={
                "company": f"Company {index}",
            },
        )

        LeadCall.objects.create(
            lead=lead,
            user=self.user,
            status="completed",
            call_name="Intro call",
            called_at=timezone.now(),
        )
        LeadReminder.objects.create(
            lead=lead,
            assigned_to=self.user,
            title="Follow up",
            due_at=timezone.now(),
            status="pending",
        )
        LeadNote.objects.create(
            lead=lead,
            created_by=self.user,
            note=f"Latest note {index}",
        )

        return lead

    def _build_context(self):
        return build_lead_table_context(
            request=self.request,
            user=self.user,
            pipeline=self.pipeline,
        )

    def test_query_count_does_not_scale_with_lead_count(self):
        self._create_lead(1)

        cache.clear()
        with self.assertNumQueries(7):
            one_lead_context = self._build_context()

        self.assertEqual(
            sum(
                group["count"]
                for group in one_lead_context["stage_groups"]
            ),
            1,
        )

        for index in range(2, 7):
            self._create_lead(index)

        cache.clear()
        with self.assertNumQueries(7):
            many_lead_context = self._build_context()

        rendered_leads = [
            lead
            for group in many_lead_context["stage_groups"]
            for lead in group["leads"]
        ]
        self.assertEqual(len(rendered_leads), 6)

        # These are the relationships the Lead Card renders. They should be
        # fully prefetched after the context builder returns.
        with self.assertNumQueries(0):
            for lead in rendered_leads:
                list(lead.calls.all())
                list(lead.lead_notes.all())
                list(lead.activities_for_card)
                _ = lead.next_reminder

    def test_attribute_cache_is_invalidated_on_definition_write(self):
        first = get_cached_attribute_definitions(
            self.organization.id
        )
        self.assertEqual(len(first), 1)

        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Budget",
            key="budget",
            display_order=1,
        )

        refreshed = get_cached_attribute_definitions(
            self.organization.id
        )
        self.assertEqual(len(refreshed), 2)
