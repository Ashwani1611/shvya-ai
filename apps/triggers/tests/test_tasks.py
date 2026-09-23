from collections import Counter

from django.test import TestCase, override_settings

from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerEvent
from apps.triggers.tasks import _fair_queryset
from services.triggers.evaluator import _timer_lead_batch, _timer_rule_batch


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


class WorkflowTimerBatchingTests(TestCase):
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
        return organization, pipeline, stage, lead

    def _rule(self, organization, pipeline, stage, suffix):
        return SmartTrigger.objects.create(
            organization=organization,
            name=f"Timer {suffix}",
            enabled=True,
            trigger_type="stage_idle",
            conditions={
                "unit": "seconds",
                "duration": 0,
                "scopes": [
                    {
                        "pipeline": str(pipeline.id),
                        "stages": [str(stage.id)],
                    }
                ],
            },
            action_type="add_note",
            action={"note": "Timer fired"},
            fingerprint=f"timer-{organization.id}-{suffix}",
        )

    @override_settings(
        WORKFLOW_TIMER_RULES_PER_PASS=2,
        WORKFLOW_TIMER_RULES_PER_ORGANIZATION=1,
    )
    def test_timer_rule_batch_reserves_space_for_each_organization(self):
        org_a, pipeline_a, stage_a, _ = self._tenant(
            "Timer Org A",
            "+919000000201",
        )
        org_b, pipeline_b, stage_b, _ = self._tenant(
            "Timer Org B",
            "+919000000202",
        )
        for index in range(3):
            self._rule(org_a, pipeline_a, stage_a, f"a-{index}")
            self._rule(org_b, pipeline_b, stage_b, f"b-{index}")

        selected = _timer_rule_batch()

        self.assertEqual(len(selected), 2)
        self.assertEqual(
            {rule.organization_id for rule in selected},
            {org_a.id, org_b.id},
        )

    @override_settings(WORKFLOW_TIMER_LEADS_PER_RULE=2)
    def test_timer_lead_cursor_advances_and_wraps_in_bounded_batches(self):
        organization, pipeline, stage, first_lead = self._tenant(
            "Timer Cursor Org",
            "+919000000211",
        )
        rule = self._rule(
            organization,
            pipeline,
            stage,
            "cursor",
        )
        for index in range(4):
            Lead.objects.create(
                organization=organization,
                pipeline=pipeline,
                stage=stage,
                name=f"Cursor Lead {index}",
                phone=f"+91900000022{index}",
            )
        leads = Lead.objects.filter(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
        )

        first = _timer_lead_batch(rule, leads)
        second = _timer_lead_batch(rule, leads)
        third = _timer_lead_batch(rule, leads)
        exhausted = _timer_lead_batch(rule, leads)
        restarted = _timer_lead_batch(rule, leads)

        traversed = [lead.id for lead in first + second + third]
        self.assertEqual([len(first), len(second), len(third)], [2, 2, 1])
        self.assertEqual(len(set(traversed)), 5)
        self.assertIn(first_lead.id, set(traversed))
        self.assertEqual(exhausted, [])
        self.assertEqual(len(restarted), 2)
