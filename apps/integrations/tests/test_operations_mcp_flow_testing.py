"""No paid provider/network calls: isolate transport, state and budget contracts."""
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.db import connection
from django.test import TestCase, TransactionTestCase

from apps.accounts.models import User
from apps.ai_engagement.models import OrgInfo, InternalConversationSummary
from apps.ai_engagement.services.credits import AICreditService, AICreditUnavailableError
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, AttributeDefinition
from apps.integrations.operations_testing_models import OperationsAIFlowRun, OperationsAIFlowTurn
from apps.integrations.operations_testing_scope import owned_test_scope
from apps.integrations.operations_testing_budget import budget_usage
from apps.integrations.operations_tools import OperationsToolError
from apps.integrations.operations.tools import flow_testing as flow
from apps.organizations.models import Organization
from apps.triggers.models import TriggerEvent
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase
from apps.integrations.operations_policy import ROLE_SUPERADMIN, ROLE_ORGANIZATION_ADMIN, CAP_DIAGNOSTICS_READ
from apps.integrations.operations_auth import OPERATIONS_READ_SCOPE
from apps.integrations.operations_models import OperationsPolicy


class FlowFixtureMixin:
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Flow test org")
        self.actor = User.objects.create_superuser(email=f"flow-{uuid.uuid4()}@example.test", password=None, name="Test operator")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = self.pipeline.stages.first()
        OrgInfo.objects.get_or_create(organization=self.org, defaults={"about": "We provide software training.", "ai_enabled": True})
        self.identity = SimpleNamespace(actor=self.actor, organization=self.org, active_organization=self.org,
            role="SHVYA_SUPERADMIN", scopes=frozenset({"operations.read", "operations.write"}),
            granted_capabilities=frozenset({flow.CAP_AI_FLOW_TEST_WRITE, "diagnostics.read"}))
        self.gate = patch.object(flow, "_write_gate", return_value=(False, "Run isolated regression tests"))
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.scope = patch.object(flow, "_organization_for", return_value=self.org)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def create_run(self, **overrides):
        args = {"idempotency_key": str(uuid.uuid4()), "pipeline_id": str(self.pipeline.pk), "stage_id": str(self.stage.pk),
            "max_turns": 5, "max_provider_calls": 10, "max_credits": 500, **overrides}
        data = flow.create_ai_flow_test_run(identity=self.identity, arguments=args).data
        return OperationsAIFlowRun.objects.get(pk=data["run_id"])

    def turn(self, run, key="turn-one", **extra):
        return flow.run_ai_flow_test_turn(identity=self.identity, arguments={
            "run_id": str(run.pk), "idempotency_key": key, "message": "Hello", **extra}).data


class OperationsAIFlowIsolationTests(FlowFixtureMixin, TestCase):
    def test_manifest_scopes_real_fixtures_without_hiding_ordinary_leads(self):
        ordinary = Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage,
            phone="+919876543210", name="Real customer")
        before_events = TriggerEvent.objects.count()
        run = self.create_run()
        other = self.create_run()
        self.assertTrue(Lead.objects.filter(pk=ordinary.pk).exists())
        self.assertFalse(Lead.objects.filter(pk=run.fixture_lead_id).exists())
        self.assertFalse(WhatsAppAccount.objects.filter(pk=run.fixture_account_id).exists())
        with owned_test_scope(run):
            lead = Lead.objects.get(pk=run.fixture_lead_id)
            self.assertEqual(lead.phone, "")
            self.assertEqual(lead.email, "")
            self.assertFalse(lead.auto_followup_enabled)
            self.assertFalse(Lead.objects.filter(pk=other.fixture_lead_id).exists())
            self.assertTrue(Lead.objects.filter(pk=ordinary.pk).exists())
            account = WhatsAppAccount.objects.get(pk=run.fixture_account_id)
            self.assertFalse(account.is_active)
            self.assertEqual(account.access_token, "")
            self.assertEqual(account.phone_number_id, "")
            lead.notes = "Scoped state update"
            lead.save(update_fields=["notes"])
        self.assertEqual(before_events, TriggerEvent.objects.count())
        self.assertFalse(Lead.objects.filter(pk=run.fixture_lead_id).exists())

    def test_create_and_turn_idempotency_and_concurrent_claim(self):
        run = self.create_run(idempotency_key="same-create")
        again = self.create_run(idempotency_key="same-create")
        self.assertEqual(run.pk, again.pk)
        with patch.object(flow, "_execute_production_turn", return_value={"summary_status": "persisted", "state": {}}) as execute:
            first = self.turn(run)
            replay = self.turn(run)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(first["turn_id"], replay["turn_id"])
            with self.assertRaises(OperationsToolError):
                self.turn(run, message="different message")
        OperationsAIFlowRun.objects.filter(pk=run.pk).update(active_turn_id=uuid.uuid4())
        with self.assertRaisesRegex(OperationsToolError, "running turn"):
            self.turn(run, key="parallel-turn")

    def test_run_never_resolves_another_tenants_stage_or_run(self):
        foreign_org = Organization.objects.create(name="Foreign")
        foreign_pipeline = Pipeline.objects.create(organization=foreign_org, name="Foreign")
        with self.assertRaises(OperationsToolError):
            self.create_run(stage_id=str(foreign_pipeline.stages.first().pk))
        run = self.create_run()
        with patch.object(flow, "_organization_for", return_value=foreign_org):
            with self.assertRaises(OperationsToolError):
                self.turn(run)

    def test_cleanup_only_manifest_fixtures_and_preserves_live_configuration(self):
        ordinary = Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage,
            phone="+919876543211", name="Keep customer")
        run = self.create_run()
        config_before = flow._configuration(self.org)
        with self.assertRaises(OperationsToolError):
            flow.cleanup_ai_flow_test_run(identity=self.identity, arguments={"run_id": str(run.pk), "lead_ids": [str(ordinary.pk)]})
        result = flow.cleanup_ai_flow_test_run(identity=self.identity, arguments={"run_id": str(run.pk)}).data
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(config_before, flow._configuration(self.org))
        self.assertTrue(Lead.objects.filter(pk=ordinary.pk).exists())
        with owned_test_scope(run):
            self.assertFalse(Lead.objects.filter(pk=run.fixture_lead_id).exists())
            self.assertFalse(WhatsAppAccount.objects.filter(pk=run.fixture_account_id).exists())
        self.assertTrue(OperationsAIFlowRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(flow.cleanup_ai_flow_test_run(identity=self.identity, arguments={"run_id": str(run.pk)}).data["idempotent_replay"])

    def test_configuration_drift_and_native_instagram_are_explicitly_rejected(self):
        with self.assertRaisesRegex(OperationsToolError, "Instagram"):
            self.create_run(channel="instagram")
        run = self.create_run()
        OrgInfo.objects.filter(organization=self.org).update(about="Configuration changed")
        with self.assertRaisesRegex(OperationsToolError, "configuration changed"):
            self.turn(run)
        OrgInfo.objects.filter(organization=self.org).delete()
        with self.assertRaisesRegex(OperationsToolError, "AI Setup"):
            self.create_run()
        self.assertFalse(OrgInfo.objects.filter(organization=self.org).exists())

    def test_real_production_persistence_summary_and_no_transport(self):
        run = self.create_run()
        decision = EngagementDecision(should_engage=True, message="Hello! How can I help?", file_document_id=None,
            crm_actions=[], reason="SALES_SUPPORT", model="mock-provider")
        with patch("apps.ai_engagement.services.turn_controller.TurnController.engage", return_value=decision), \
             patch("apps.ai_engagement.services.internal_summary.InternalSummaryService.generate_summary", return_value=("Customer greeted us.", "mock-provider")), \
             patch("celery.app.task.Task.apply_async") as queue:
            result = self.turn(run)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["messages_sent"], 0)
        self.assertFalse(result["live_delivery_test"])
        self.assertEqual(result["result"]["state"]["summary"], "Customer greeted us.")
        self.assertTrue(result["result"]["state"]["persisted"])
        queue.assert_not_called()
        self.assertEqual(WhatsAppMessage.objects.count(), 0)
        with owned_test_scope(run):
            self.assertEqual(WhatsAppMessage.objects.filter(lead_id=run.fixture_lead_id).count(), 2)
            outbound = WhatsAppMessage.objects.get(lead_id=run.fixture_lead_id, direction="outbound")
            self.assertEqual(outbound.status, "failed")
            self.assertIn("transport intentionally disabled", outbound.error)
            self.assertTrue(InternalConversationSummary.objects.filter(lead_id=run.fixture_lead_id, is_active=True).exists())

    def test_two_turn_question_ledger_and_attribute_persistence(self):
        from tests.playbook_fixtures import build_ai_playbook
        OrgInfo.objects.filter(organization=self.org).update(ai_playbook=build_ai_playbook(questions="Which city?\nWhat is your budget?"))
        AttributeDefinition.objects.create(organization=self.org, name="City", key="city")
        run = self.create_run()
        def answer(*, organization, lead, **kwargs):
            source = lead.whatsapp_messages.filter(direction="inbound").order_by("-created_at").first()
            if source.body == "Hello":
                return EngagementDecision(should_engage=True, message="Which city?", file_document_id=None,
                    crm_actions=[], qualification_updates=[], next_requirement_id="which_city",
                    reason="QUALIFICATION_NEXT", reason_code="QUALIFICATION_NEXT", model="mock-provider")
            return EngagementDecision(should_engage=True, message="Thanks. What is your budget?", file_document_id=None,
                crm_actions=[{"type": "attribute_updates", "updates": [{"key": "city", "value": "Delhi"}]}],
                qualification_updates=[{"requirement_id": "which_city", "value": "Delhi", "source_message_id": str(source.pk), "evidence": "Delhi"}],
                next_requirement_id="what_is_your_budget", reason="QUALIFICATION_NEXT", reason_code="QUALIFICATION_NEXT", model="mock-provider")
        with patch("apps.ai_engagement.services.turn_controller.TurnController.engage", side_effect=answer), \
             patch("apps.ai_engagement.services.internal_summary.InternalSummaryService.generate_summary", return_value=("Customer is in Delhi.", "mock-provider")):
            first = self.turn(run)
            self.assertEqual(first["status"], "completed", first)
            self.assertEqual(first["result"]["state"]["qualification"]["last_asked_requirement_id"], "which_city")
            second = self.turn(run, key="second", message="Delhi")
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["result"]["state"]["attributes"]["city"], "Delhi")
        self.assertEqual(second["result"]["state"]["qualification"]["requirement_states"]["which_city"]["status"], "answered")

    def test_test_executor_rejects_shared_schema_and_external_actions(self):
        from apps.ai_engagement.services.crm_executor import CRMActionExecutor, CRMActionExecutionError
        run = self.create_run()
        before = AttributeDefinition.objects.filter(organization=self.org).count()
        with owned_test_scope(run):
            lead = Lead.objects.get(pk=run.fixture_lead_id)
            with self.assertRaisesRegex(CRMActionExecutionError, "shared attribute"):
                CRMActionExecutor().execute(organization=self.org, lead=lead, actions=[{
                    "type": "attribute_updates", "updates": [{"key": "new_test_field", "value": "test", "create_if_missing": True, "name": "New test field"}]}])
            with self.assertRaisesRegex(CRMActionExecutionError, "External/calendar"):
                CRMActionExecutor().execute(organization=self.org, lead=lead, actions=[{"type": "create_reminder"}])
            with patch("apps.shvya_calendar.attribute_sync.sync_manual_booking") as booking:
                with self.assertRaisesRegex(CRMActionExecutionError, "calendar booking"):
                    CRMActionExecutor().execute(organization=self.org, lead=lead, actions=[{"type": "attribute_updates", "updates": [{"key": "booked_at", "value": "2026-10-10T10:00:00Z"}]}])
                booking.assert_not_called()
        self.assertEqual(AttributeDefinition.objects.filter(organization=self.org).count(), before)

    def test_provider_budget_includes_retries_and_settled_actual_cost(self):
        run = self.create_run(max_provider_calls=2, max_credits=10)
        OperationsAIFlowRun.objects.filter(pk=run.pk).update(active_turn_id=uuid.uuid4())
        AICreditService.add_manual_credits(organization=self.org, amount=100, actor=self.actor, reason="Test wallet")
        with owned_test_scope(run):
            first = AICreditService._reserve(organization_id=self.org.pk, model="mock", feature="engagement", reference_id="a",
                credits=6, estimated_input_tokens=1, estimated_output_tokens=1)
            with self.assertRaises(AICreditUnavailableError):
                AICreditService._reserve(organization_id=self.org.pk, model="mock", feature="summary", reference_id="b",
                    credits=5, estimated_input_tokens=1, estimated_output_tokens=1)
            AICreditService.settle(reservation=first, input_tokens=1, output_tokens=1, charge_override=2)
            second = AICreditService._reserve(organization_id=self.org.pk, model="mock", feature="summary", reference_id="c",
                credits=5, estimated_input_tokens=1, estimated_output_tokens=1)
            AICreditService.release(second)
            with self.assertRaisesRegex(AICreditUnavailableError, "provider-call budget"):
                AICreditService._reserve(organization_id=self.org.pk, model="mock", feature="retry", reference_id="d",
                    credits=1, estimated_input_tokens=1, estimated_output_tokens=1)
        run.refresh_from_db()
        self.assertEqual(budget_usage(run)["settled_credits"], 2)
        self.assertEqual(budget_usage(run)["provider_calls"], 2)


class OperationsAIFlowDurabilityTests(FlowFixtureMixin, TransactionTestCase):
    def test_endpoint_runs_billable_work_outside_atomic_request(self):
        from apps.integrations.operations_models import OperationsOAuthClient
        from apps.integrations.operations_policy import ROLE_SUPERADMIN
        from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase
        run = self.create_run()
        self.gate.stop()
        self.scope.stop()
        self.oauth_client = OperationsOAuthClient.objects.create(client_id="flow-durable-client", client_name="Flow durable",
            redirect_uris=["https://chatgpt.com/aip/callback"], grant_types=["authorization_code", "refresh_token"], response_types=["code"])
        bearer = OperationsMCPBase._token(self, actor=self.actor, role=ROLE_SUPERADMIN)
        def invoke(name, arguments):
            return OperationsMCPBase._result(self, OperationsMCPBase._call(self, bearer, name, arguments))
        selected = invoke("select_organization_context", {"organization_id": str(self.org.pk), "reason": "Select test organization"})
        self.assertFalse(selected["isError"], selected)
        args = {"run_id": str(run.pk), "idempotency_key": "durable-endpoint", "message": "Hello",
            "reason": "Verify durable isolated billing boundary"}
        preview = invoke("run_ai_flow_test_turn", {**args, "dry_run": True})
        self.assertFalse(preview["isError"], preview)
        def execute(current_run, turn):
            self.assertFalse(connection.in_atomic_block)
            persisted = OperationsAIFlowRun.objects.get(pk=current_run.pk)
            self.assertEqual(persisted.active_turn_id, turn.pk)
            return {"summary_status": "persisted", "state": {}}
        with patch.object(flow, "_execute_production_turn", side_effect=execute) as generated:
            result = invoke("run_ai_flow_test_turn", {**args, "dry_run": False, "approved": True,
                "approval_event_id": preview["structuredContent"]["approval_event_id"]})
        self.assertFalse(result["isError"], result)
        self.assertEqual(generated.call_count, 1)

    def test_process_interruption_retains_committed_claim_without_replaying(self):
        run = self.create_run()
        def interrupted(current_run, turn):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(OperationsAIFlowTurn.objects.filter(pk=turn.pk, status="running").exists())
            raise KeyboardInterrupt("simulated worker death")
        with patch.object(flow, "_execute_production_turn", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.turn(run)
        run.refresh_from_db()
        self.assertIsNotNone(run.active_turn_id)
        replay = self.turn(run)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["status"], "running")


class OperationsAIFlowEndpointTests(OperationsMCPBase):
    def _flow_bearer(self):
        OrgInfo.objects.get_or_create(organization=self.organization, defaults={"about": "Company test profile"})
        bearer = self._token(actor=self.superadmin, role=ROLE_SUPERADMIN)
        selected = self._result(self._call(bearer, "select_organization_context", {
            "organization_id": str(self.organization.pk), "reason": "Prepare isolated AI tests"}))
        self.assertFalse(selected["isError"], selected)
        return bearer

    def _approved(self, bearer, tool, arguments):
        preview = self._result(self._call(bearer, tool, {**arguments, "dry_run": True}))
        self.assertFalse(preview["isError"], preview)
        data = preview["structuredContent"]
        self.assertTrue(data["approval_required"])
        applied = self._result(self._call(bearer, tool, {**arguments, "dry_run": False,
            "approved": True, "approval_event_id": data["approval_event_id"]}))
        self.assertFalse(applied["isError"], applied)
        return applied["structuredContent"], data["approval_event_id"]

    def test_full_endpoint_approval_lifecycle_and_single_use(self):
        bearer = self._flow_bearer()
        args = {"pipeline_id": str(self.pipeline.pk), "stage_id": str(self.new_stage.pk),
            "idempotency_key": "endpoint-run", "reason": "Test production AI with isolated fixtures"}
        blocked = self._result(self._call(bearer, "create_ai_flow_test_run", {**args, "dry_run": False}))
        self.assertTrue(blocked["isError"])
        run, approval = self._approved(bearer, "create_ai_flow_test_run", args)
        replay = self._result(self._call(bearer, "create_ai_flow_test_run", {**args, "dry_run": False,
            "approved": True, "approval_event_id": approval}))
        self.assertTrue(replay["isError"], replay)
        turn_args = {"run_id": run["run_id"], "idempotency_key": "endpoint-turn", "message": "Hello",
            "reason": "Execute the approved isolated AI test turn"}
        with patch.object(flow, "_execute_production_turn", return_value={"summary_status": "persisted", "state": {}}) as execute:
            turn, _ = self._approved(bearer, "run_ai_flow_test_turn", turn_args)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(turn["messages_sent"], 0)
        inspected = self._result(self._call(bearer, "get_ai_flow_test_run", {"run_id": run["run_id"]}))
        self.assertFalse(inspected["isError"], inspected)
        self.assertEqual(inspected["structuredContent"]["turn_count"], 1)
        cleaned, _ = self._approved(bearer, "cleanup_ai_flow_test_run", {"run_id": run["run_id"],
            "reason": "Clean only this completed test manifest"})
        self.assertTrue(cleaned["cleanup_complete"])

    def test_endpoint_rejects_write_scope_and_foreign_stage(self):
        OperationsPolicy.objects.create(organization=self.organization, organization_admin_enabled=True,
            allowed_capabilities=[flow.CAP_AI_FLOW_TEST_WRITE, CAP_DIAGNOSTICS_READ])
        bearer = self._token(actor=self.admin, role=ROLE_ORGANIZATION_ADMIN, organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE], granted_capabilities=[flow.CAP_AI_FLOW_TEST_WRITE, CAP_DIAGNOSTICS_READ])
        args = {"pipeline_id": str(self.pipeline.pk), "stage_id": str(self.other_lead.stage_id),
            "idempotency_key": "denied-run", "reason": "Check authorization for test creation", "dry_run": True}
        denied = self._result(self._call(bearer, "create_ai_flow_test_run", args))
        self.assertTrue(denied["isError"])
        self.assertEqual(OperationsAIFlowRun.objects.count(), 0)
