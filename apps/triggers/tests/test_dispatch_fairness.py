import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.crm.models import Lead
from apps.organizations.models import Organization
from apps.triggers.models import TriggerEvent
from apps.triggers.tasks import _fair_queryset


LOC_MEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "workflow-fairness",
    }
}


@override_settings(CACHES=LOC_MEM_CACHE)
class WorkflowTenantFairnessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.events = []
        for index in range(3):
            organization = Organization.objects.create(
                package="dfy",
                name=f"Fair Workflow Org {index}",
            )
            pipeline = organization.pipelines.first()
            stage = pipeline.stages.order_by("display_order").first()
            lead = Lead.objects.create(
                organization=organization,
                pipeline=pipeline,
                stage=stage,
                name=f"Lead {index}",
                phone=f"+91900000000{index}",
            )
            self.events.append(
                TriggerEvent.objects.create(
                    organization=organization,
                    lead=lead,
                    kind="lead_created",
                    key=f"workflow-fairness:{uuid.uuid4()}",
                    payload={},
                )
            )

    def test_fair_queryset_rotates_past_first_organization_batch(self):
        pending = TriggerEvent.objects.filter(processed_at__isnull=True)
        first = list(
            _fair_queryset(
                pending,
                partition_by="organization_id",
                order_by="created_at",
                per_organization=1,
                limit=2,
                cursor_key="test:workflow-fairness",
            ).values_list("organization_id", flat=True)
        )
        second = list(
            _fair_queryset(
                pending,
                partition_by="organization_id",
                order_by="created_at",
                per_organization=1,
                limit=2,
                cursor_key="test:workflow-fairness",
            ).values_list("organization_id", flat=True)
        )

        self.assertEqual(len(first), 2)
        self.assertEqual(len(set(first)), 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(len(set(second)), 2)
        self.assertEqual(
            set(first) | set(second),
            {event.organization_id for event in self.events},
        )
