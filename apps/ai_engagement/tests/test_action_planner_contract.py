from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.ai_engagement.services.action_plan_trace import trace_action_plan
from apps.ai_engagement.services.action_planner import ActionPlan, ActionPlanner, ActionProposal
from apps.ai_engagement.services.crm_executor import (
    CRMActionExecutionError,
    CRMActionExecutor,
)
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class ActionPlannerContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Phase 7 Contract Org")
        cls.owner = User.objects.create_user(
            email="phase7-contract@example.com",
            password="password",
            name="Owner",
            organization=cls.organization,
        )
        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Pipeline A",
            description="",
            owner=cls.owner,
            is_active=True,
        )
        cls.pipeline_b = Pipeline.objects.create(
            organization=cls.organization,
            name="Pipeline B",
            description="",
            owner=cls.owner,
            is_active=True,
        )
        cls.stage_a = Stage.objects.filter(
            pipeline=cls.pipeline,
            is_active=True,
        ).order_by("display_order").first()
        cls.stage_b = Stage.objects.filter(
            pipeline=cls.pipeline_b,
            is_active=True,
        ).order_by("display_order").first()

    def setUp(self):
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage_a,
            name="Contract Lead",
            phone="+919999999911",
            email="contract@example.com",
            attributes={},
        )
        self.source = SimpleNamespace(id="message-7", body="Please follow up.")
        self.planner = ActionPlanner()

    @staticmethod
    def decision(actions):
        return SimpleNamespace(
            crm_actions=actions,
            file_document_id=None,
            reason_code="NORMAL_CONVERSATION",
        )

    def test_valid_cross_pipeline_transition_is_planned_by_target_stage(self):
        plan = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision([{
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": str(self.stage_b.id)},
            }]),
        )
        self.assertEqual(len(plan.accepted_actions), 1)
        self.assertEqual(plan.executor_actions[0]["stage_shift"]["stage_id"], str(self.stage_b.id))

    def test_transition_snapshot_contains_current_pipeline_and_stage(self):
        plan = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision([{
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": str(self.stage_b.id)},
            }]),
        )
        snapshot = plan.accepted_actions[0].state_snapshot
        self.assertEqual(snapshot["pipeline_id"], str(self.pipeline.id))
        self.assertEqual(snapshot["stage_id"], str(self.stage_a.id))
        self.assertTrue(snapshot["runtime_profile_revision"])

    def test_stale_transition_snapshot_is_rejected_before_execution(self):
        plan = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision([{
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": str(self.stage_b.id)},
            }]),
        )
        self.lead.pipeline = self.pipeline_b
        self.lead.stage = self.stage_b
        with self.assertRaises(CRMActionExecutionError):
            CRMActionExecutor._revalidate_plan_state(plan=plan, lead=self.lead)

    def test_unchanged_transition_snapshot_passes_revalidation(self):
        plan = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision([{
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": str(self.stage_b.id)},
            }]),
        )
        CRMActionExecutor._revalidate_plan_state(plan=plan, lead=self.lead)

    def test_note_idempotency_key_changes_when_source_message_changes(self):
        action = [{"type": "add_note", "note": "Follow up requested"}]
        first = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision(action),
        ).actions[0]
        second = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=SimpleNamespace(id="message-8", body="Please follow up."),
            decision=self.decision(action),
        ).actions[0]
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)

    def test_reminder_idempotency_key_is_stable_for_same_source(self):
        action = [{
            "type": "create_reminder",
            "title": "Call",
            "description": "Requested by customer",
            "due_at": "2026-09-18T10:00:00+05:30",
        }]
        first = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision(action),
        ).actions[0]
        second = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision(action),
        ).actions[0]
        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_equivalent_transport_state_produces_equivalent_executor_actions(self):
        action = [{"type": "add_note", "note": "Equivalent transport proposal"}]
        api_plan = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision(action),
            source_policy="api",
        )
        hosted_plan = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=self.source,
            decision=self.decision(action),
            source_policy="hosted",
        )
        self.assertEqual(api_plan.executor_actions, hosted_plan.executor_actions)

    def test_action_plan_trace_records_proposals_and_planning_latency(self):
        proposal = ActionProposal(
            action_type="ADD_NOTE",
            executor_action_type="add_note",
            parameters={"note": "x"},
            organization_id=str(self.organization.id),
            lead_id=str(self.lead.id),
            source_evidence=({"source": "customer_message"},),
            reason="explicit request",
            confidence="high",
            source_intent="FOLLOW_UP",
            source_policy="continue",
            idempotency_key="key",
            required_permissions=("ai_side_effects",),
            validation_status="ACCEPTED",
            reason_code="VALIDATED",
            state_snapshot={},
        )
        plan = ActionPlan(
            actions=(proposal,),
            organization_id=str(self.organization.id),
            lead_id=str(self.lead.id),
            source_message_id="message-7",
            policy_outcome="continue",
            plan_reason="test",
            planning_latency_ms=1.25,
        )
        with patch("apps.ai_engagement.services.trace_service.append") as append, patch(
            "apps.ai_engagement.services.trace_service.record"
        ) as record:
            trace_action_plan(plan)
        append.assert_called_once()
        record.assert_called_once_with("performance", {"action_planning_ms": 1.25})

    def test_trace_failure_does_not_break_action_processing(self):
        plan = ActionPlan(
            actions=(),
            organization_id=str(self.organization.id),
            lead_id=str(self.lead.id),
            source_message_id="message-7",
            policy_outcome="continue",
            plan_reason="test",
            planning_latency_ms=0.1,
        )
        with patch(
            "apps.ai_engagement.services.trace_service.append",
            side_effect=RuntimeError("trace unavailable"),
        ):
            trace_action_plan(plan)

    def test_booking_is_not_fabricated_without_existing_booking_executor(self):
        plan = self.planner.plan(
            organization=self.organization,
            lead=self.lead,
            source_message=SimpleNamespace(id="message-9", body="Can I book tomorrow?"),
            decision=self.decision([]),
        )
        self.assertFalse(any(item.action_type == "BOOKING_REQUEST" for item in plan.actions))
        self.assertEqual(plan.executor_actions, [])
