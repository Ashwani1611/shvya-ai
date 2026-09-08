from datetime import timedelta

from django.http import QueryDict
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import AttributeDefinition, Lead, LeadNote, Pipeline, Stage
from apps.crm.models.activity import LeadActivity
from apps.organizations.models import Organization
from services.crm.lead_filter_service import (
    apply_lead_filters,
    public_attribute_definitions,
)


class LeadFilterServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Filter Org")
        self.user = User.objects.create_user(
            email="filters@example.com",
            password="test-password",
            name="Filter Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline_a = Pipeline.objects.create(organization=self.org, name="Sales")
        self.pipeline_b = Pipeline.objects.create(organization=self.org, name="Renewals")
        self.stage_a = Stage.objects.create(pipeline=self.pipeline_a, name="New")
        self.stage_b = Stage.objects.get(pipeline=self.pipeline_b, name="Qualified")
        self.lead_a = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline_a,
            stage=self.stage_a,
            name="Ash Lead",
            phone="+919000001001",
            attributes={"city": "Noida", "_shvya_ai_qualification": "internal"},
        )
        self.lead_b = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline_b,
            stage=self.stage_b,
            name="Other Lead",
            phone="+919000001002",
            attributes={"city": "Gurugram"},
        )
        AttributeDefinition.objects.create(
            organization=self.org,
            name="City",
            key="city",
        )
        AttributeDefinition.objects.create(
            organization=self.org,
            name="Internal qualification",
            key="_shvya_ai_qualification",
        )

    def _filter(self, **params):
        query = QueryDict("", mutable=True)
        query.update({key: str(value) for key, value in params.items()})
        return apply_lead_filters(
            Lead.objects.filter(organization=self.org),
            query,
            user=self.user,
        )

    def test_notes_filter_matches_words_in_note_history(self):
        LeadNote.objects.create(
            lead=self.lead_a,
            created_by=self.user,
            note="Customer asked about premium pricing and onboarding.",
            note_type="manual",
        )
        self.assertEqual(
            list(self._filter(filter_notes="premium")),
            [self.lead_a],
        )

    def test_stage_and_pipeline_filters_work(self):
        self.assertEqual(
            list(self._filter(filter_stage=self.stage_b.id)),
            [self.lead_b],
        )
        self.assertEqual(
            list(self._filter(filter_pipeline=self.pipeline_a.id)),
            [self.lead_a],
        )
        self.assertEqual(
            set(self._filter(filter_pipeline="all")),
            {self.lead_a, self.lead_b},
        )

    def test_custom_attribute_filter_and_internal_attribute_exclusion(self):
        definitions = list(public_attribute_definitions(self.org))
        self.assertEqual([item.key for item in definitions], ["city"])
        self.assertEqual(
            list(self._filter(attr_city="noid")),
            [self.lead_a],
        )

    def test_ai_qualified_date_uses_ai_qualification_note(self):
        LeadNote.objects.create(
            lead=self.lead_b,
            note="<AI Qualification Summary - now>\nQualified based on requirements.",
            note_type="system",
        )
        today = timezone.localdate().isoformat()
        self.assertEqual(
            list(self._filter(filter_ai_qualified_date=today)),
            [self.lead_b],
        )

    def test_days_in_stage_and_pipeline(self):
        old = timezone.now() - timedelta(days=8)
        Lead.objects.filter(pk=self.lead_a.pk).update(stage_entered_at=old)
        self.lead_a.refresh_from_db()
        activity = LeadActivity.objects.create(
            lead=self.lead_a,
            organization=self.org,
            topic=LeadActivity.Topic.PIPELINE_CHANGED,
            new_pipeline=self.pipeline_a,
            new_pipeline_name=self.pipeline_a.name,
        )
        LeadActivity.objects.filter(pk=activity.pk).update(created_at=old)

        self.assertEqual(
            list(self._filter(filter_days_in_stage=7)),
            [self.lead_a],
        )
        self.assertEqual(
            list(self._filter(filter_days_in_pipeline=7)),
            [self.lead_a],
        )
