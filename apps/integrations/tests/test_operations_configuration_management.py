import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_engagement.models import FAQ
from apps.crm.models import AttributeDefinition, Pipeline
from apps.followups.models import TouchpointCategory, TouchpointReply
from apps.integrations.models import (
    OperationsConfigurationPlan,
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsPolicy,
)
from apps.integrations.operations_auth import (
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    token_hash,
)
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_CONFIGURATION_PLAN_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
    ROLE_ORGANIZATION_ADMIN,
    capabilities_for_grant,
)
from apps.organizations.models import Organization


class OperationsConfigurationManagementTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Plan Org")
        self.admin = User.objects.create_user(
            email="plan-admin@example.test",
            organization=self.organization,
            password=None,
            name="Plan Admin",
            role=User.Role.ADMIN,
        )
        self._policy(self.organization, self.admin)
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
        )
        self.new_stage = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.client_record = OperationsOAuthClient.objects.create(
            client_id="configuration_plan_test_client",
            client_name="Configuration Plan Test",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        self.bearer = self._token(
            organization=self.organization,
            actor=self.admin,
            raw="plan-bearer",
        )

    def _policy(self, organization, actor):
        return OperationsPolicy.objects.create(
            organization=organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
                CAP_AI_CONFIG_WRITE,
                CAP_PIPELINE_CONFIG_WRITE,
                CAP_STAGE_CONFIG_WRITE,
                CAP_ATTRIBUTE_CONFIG_WRITE,
                CAP_WORKFLOW_CONFIG_WRITE,
                CAP_CADENCE_CONFIG_WRITE,
                CAP_MESSAGING_CONFIG_WRITE,
                CAP_CONFIGURATION_PLAN_WRITE,
            ],
            approval_required_capabilities=[
                CAP_CONFIGURATION_PLAN_WRITE,
            ],
            updated_by=actor,
        )

    def _token(self, *, organization, actor, raw):
        scopes = [OPERATIONS_READ_SCOPE, OPERATIONS_WRITE_SCOPE]
        granted = sorted(
            capabilities_for_grant(
                role=ROLE_ORGANIZATION_ADMIN,
                organization=organization,
                allow_writes=True,
            )
        )
        OperationsOAuthToken.objects.create(
            client=self.client_record,
            actor=actor,
            organization=organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash(raw),
            refresh_token_hash=token_hash(raw + "-refresh"),
            scope=" ".join(scopes),
            granted_capabilities=granted,
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=4),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        return raw

    def _call(self, bearer, name, arguments=None):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": arguments or {},
                    },
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + bearer,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def _ok(self, bearer, name, arguments=None):
        result = self._call(bearer, name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_p1_tools_are_discoverable(self):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "tools",
                    "method": "tools/list",
                    "params": {},
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.bearer,
        )
        tools = {item["name"]: item for item in response.json()["result"]["tools"]}
        for name in (
            "get_configuration_dependency_graph",
            "validate_organization_configuration",
            "reorder_stages",
            "create_configuration_plan",
            "apply_configuration_plan",
            "rollback_configuration_plan",
            "export_organization_configuration",
            "import_organization_configuration",
        ):
            self.assertIn(name, tools)

    def test_dependency_graph_and_validation_return_configuration_etag(self):
        graph = self._ok(
            self.bearer,
            "get_configuration_dependency_graph",
        )
        self.assertEqual(len(graph["configuration_etag"]), 64)
        self.assertTrue(
            any(
                node["type"] == "pipeline" and node["id"] == str(self.pipeline.id)
                for node in graph["nodes"]
            )
        )
        validation = self._ok(
            self.bearer,
            "validate_organization_configuration",
        )
        self.assertEqual(len(validation["configuration_etag"]), 64)
        self.assertIn("errors", validation)
        self.assertIn("warnings", validation)

    def test_atomic_plan_uses_one_approval_and_can_rollback_recoverable_updates(self):
        original_pipeline_description = self.pipeline.description
        original_stage_description = self.new_stage.description

        created = self._ok(
            self.bearer,
            "create_configuration_plan",
            {
                "reason": "Update sales pipeline and New leads descriptions together.",
                "operations": [
                    {
                        "ref": "pipeline",
                        "tool": "upsert_pipeline_configuration",
                        "arguments": {
                            "pipeline_id": str(self.pipeline.id),
                            "data": {
                                "name": self.pipeline.name,
                                "description": "Updated by plan",
                                "is_active": True,
                                "ai_enabled": True,
                            },
                        },
                    },
                    {
                        "ref": "stage",
                        "tool": "upsert_stage_configuration",
                        "arguments": {
                            "pipeline_id": str(self.pipeline.id),
                            "stage_id": str(self.new_stage.id),
                            "data": {
                                "name": self.new_stage.name,
                                "description": "Updated stage by plan",
                                "display_order": self.new_stage.display_order,
                                "is_active": True,
                                "ai_on": True,
                            },
                        },
                    },
                ],
            },
        )
        self.assertTrue(created["approval_required"])
        self.assertTrue(created["reversible_after_success"])
        self.assertIn("approval_event_id", created)
        self.assertEqual(len(created["base_configuration_etag"]), 64)

        applied = self._ok(
            self.bearer,
            "apply_configuration_plan",
            {
                "plan_id": created["plan_id"],
                "approved": True,
                "approval_event_id": created["approval_event_id"],
                "reason": "Apply approved pipeline and stage configuration plan.",
            },
        )
        self.assertEqual(applied["status"], "APPLIED")
        self.pipeline.refresh_from_db()
        self.new_stage.refresh_from_db()
        self.assertEqual(self.pipeline.description, "Updated by plan")
        self.assertEqual(self.new_stage.description, "Updated stage by plan")

        rollback_preview = self._ok(
            self.bearer,
            "rollback_configuration_plan",
            {
                "plan_id": created["plan_id"],
                "reason": "Review rollback of the applied configuration plan.",
            },
        )
        self.assertIn("approval_event_id", rollback_preview)

        rolled_back = self._ok(
            self.bearer,
            "rollback_configuration_plan",
            {
                "plan_id": created["plan_id"],
                "dry_run": False,
                "approved": True,
                "approval_event_id": rollback_preview["approval_event_id"],
                "reason": "Rollback approved configuration plan after verification.",
            },
        )
        self.assertEqual(rolled_back["status"], "ROLLED_BACK")
        self.pipeline.refresh_from_db()
        self.new_stage.refresh_from_db()
        self.assertEqual(self.pipeline.description, original_pipeline_description)
        self.assertEqual(self.new_stage.description, original_stage_description)
        plan = OperationsConfigurationPlan.objects.get(pk=created["plan_id"])
        self.assertEqual(plan.status, OperationsConfigurationPlan.Status.ROLLED_BACK)

    def test_member_failure_rolls_back_all_prior_plan_writes_and_consumes_receipt(self):
        original = self.pipeline.description
        created = self._ok(
            self.bearer,
            "create_configuration_plan",
            {
                "reason": "Test atomic configuration rollback on a deferred member failure.",
                "operations": [
                    {
                        "ref": "pipeline",
                        "tool": "upsert_pipeline_configuration",
                        "arguments": {
                            "pipeline_id": str(self.pipeline.id),
                            "data": {
                                "name": self.pipeline.name,
                                "description": "Must roll back",
                                "is_active": True,
                                "ai_enabled": True,
                            },
                        },
                    },
                    {
                        "ref": "bad_stage",
                        "tool": "upsert_stage_configuration",
                        "arguments": {
                            "pipeline_id": {"$ref": "pipeline.target_id"},
                            "stage_id": str(self.new_stage.id),
                            "data": {
                                "name": self.new_stage.name,
                                "description": "Invalid order",
                                "display_order": self.qualified.display_order,
                                "is_active": True,
                                "ai_on": True,
                            },
                        },
                    },
                ],
            },
        )
        result = self._call(
            self.bearer,
            "apply_configuration_plan",
            {
                "plan_id": created["plan_id"],
                "approved": True,
                "approval_event_id": created["approval_event_id"],
                "reason": "Execute atomic failure test configuration plan.",
            },
        )
        self.assertTrue(result["isError"])
        self.pipeline.refresh_from_db()
        self.assertEqual(self.pipeline.description, original)

        retry = self._call(
            self.bearer,
            "apply_configuration_plan",
            {
                "plan_id": created["plan_id"],
                "approved": True,
                "approval_event_id": created["approval_event_id"],
                "reason": "Retry atomic failure test with consumed approval receipt.",
            },
        )
        self.assertTrue(retry["isError"])
        self.assertEqual(
            retry["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

    def test_reorder_stages_is_atomic_and_etag_changes(self):
        before = self._ok(
            self.bearer,
            "export_organization_configuration",
        )
        stages = list(self.pipeline.stages.order_by("display_order", "name"))
        order = [str(stage.id) for stage in reversed(stages)]

        preview = self._ok(
            self.bearer,
            "reorder_stages",
            {
                "pipeline_id": str(self.pipeline.id),
                "stage_ids": order,
                "reason": "Review atomic stage reorder for the Sales pipeline.",
            },
        )
        self.assertEqual(preview["status"], "DRY_RUN")

        changed = self._ok(
            self.bearer,
            "reorder_stages",
            {
                "pipeline_id": str(self.pipeline.id),
                "stage_ids": order,
                "dry_run": False,
                "approved": False,
                "reason": "Apply atomic stage reorder for the Sales pipeline.",
            },
        )
        self.assertEqual(changed["stage_ids"], order)
        after = self._ok(
            self.bearer,
            "export_organization_configuration",
        )
        self.assertNotEqual(
            before["configuration_etag"],
            after["configuration_etag"],
        )

    def test_portable_export_import_creates_approval_plan_without_credentials(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Cuisine Preference",
            key="cuisine_preference",
            field_type="text",
            description="Preferred cuisine.",
        )
        FAQ.objects.create(
            organization=self.organization,
            question="Do you take reservations?",
            answer="Yes.",
        )
        category = TouchpointCategory.objects.create(
            organization=self.organization,
            name="Reservations",
        )
        TouchpointReply.objects.create(
            category=category,
            title="Ask party size",
            body="How many guests should I reserve for?",
        )

        exported = self._ok(
            self.bearer,
            "export_organization_configuration",
        )
        raw_export = json.dumps(exported)
        self.assertNotIn("access_token", raw_export)
        self.assertNotIn("refresh_token", raw_export)
        self.assertFalse(exported["secret_material_included"])

        target = Organization.objects.create(name="Imported Plan Org")
        target_admin = User.objects.create_user(
            email="import-admin@example.test",
            organization=target,
            password=None,
            name="Import Admin",
            role=User.Role.ADMIN,
        )
        self._policy(target, target_admin)
        target_bearer = self._token(
            organization=target,
            actor=target_admin,
            raw="import-bearer",
        )

        imported = self._ok(
            target_bearer,
            "import_organization_configuration",
            {
                "configuration": exported["configuration"],
                "reason": "Import reviewed CRM and AI configuration template into target organization.",
            },
        )
        self.assertEqual(imported["status"], "DRY_RUN")
        self.assertIn("approval_event_id", imported)
        self.assertFalse(imported["reversible_after_success"])

        applied = self._ok(
            target_bearer,
            "apply_configuration_plan",
            {
                "plan_id": imported["plan_id"],
                "approved": True,
                "approval_event_id": imported["approval_event_id"],
                "reason": "Apply approved imported organization configuration template.",
            },
        )
        self.assertEqual(applied["status"], "APPLIED")
        self.assertTrue(
            Pipeline.objects.filter(
                organization=target,
                name="Sales",
            ).exists()
        )
        self.assertTrue(
            AttributeDefinition.objects.filter(
                organization=target,
                key="cuisine_preference",
            ).exists()
        )
        self.assertTrue(
            FAQ.objects.filter(
                organization=target,
                question="Do you take reservations?",
            ).exists()
        )
        self.assertTrue(
            TouchpointReply.objects.filter(
                category__organization=target,
                title="Ask party size",
            ).exists()
        )
