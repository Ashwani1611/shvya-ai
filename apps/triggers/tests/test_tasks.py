from collections import Counter

from django.test import TestCase

from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from apps.triggers.models import TriggerEvent
from apps.triggers.tasks import _fair_queryset


class WorkflowDispatchFairnessTests(TestCase):
    def _tenant(self, name, phone):
        organization = Organization.objects.create(name=name)
        pipeline = Pipeline.objects.create(
            organization=organization,
            name="Leads",
        )
        stage = Stage.objects.create(
            pipeline=pipeline,
            name="New",
            display_order=0,
        )
        lead = Lead.objects.create(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
            name=f"{name} Lead",
            phone=phone,
        )
        return organization, lead

    def test_fair_queryset_caps_each_organization_inside_global_batch(self):
        org_a, lead_a = self._tenant("Workflow A", "+919000000101")
        org_b, lead_b = self._tenant("Workflow B", "+919000000102")

        for index in range(8):
            TriggerEvent.objects.create(
                organization=org_a,
                lead=lead_a,
                kind="lead_created",
                key=f"org-a-{index}",
            )
        for index in range(3):
            TriggerEvent.objects.create(
                organization=org_b,
                lead=lead_b,
                kind="lead_created",
                key=f"org-b-{index}",
            )

        selected = list(
            _fair_queryset(
                TriggerEvent.objects.filter(processed_at__isnull=True),
                partition_by="organization_id",
                order_by="created_at",
                per_organization=2,
                limit=4,
            ).values_list("organization_id", flat=True)
        )

        counts = Counter(selected)
        self.assertEqual(len(selected), 4)
        self.assertEqual(counts[org_a.id], 2)
        self.assertEqual(counts[org_b.id], 2)
