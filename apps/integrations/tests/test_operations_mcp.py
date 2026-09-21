import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.ai_engagement.models import OrgInfo
from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, Pipeline, Stage
from apps.followups.models import FollowupSequence, FollowupStep
from apps.integrations.operations_agent_prompt import OPERATIONS_AGENT_INSTRUCTIONS
from apps.integrations.models import (
    OperationsApprovalUse,
    OperationsAuditEvent,
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsPolicy,
    OperationsSupportSession,
)
from apps.integrations.operations_auth import (
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    pkce_s256,
    token_hash,
)
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_AUTOMATION_CONFIG_WRITE,
    CAP_CRM_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_LEAD_STAGE_WRITE,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
)
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger
from services.crm.lead_transition import move_lead_to_stage


class OperationsMCPTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Operations Org A")
        self.other_organization = Organization.objects.create(name="Operations Org B")
        self.admin = User.objects.create_user(
            email="org-admin@example.test",
            organization=self.organization,
            password=None,
            name="Organization Admin",
            role=User.Role.ADMIN,
        )
        self.superadmin = User.objects.create_superuser(
            email="superadmin@example.test",
            password=None,
            name="SHVYA Support",
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
        )
        self.new_stage = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.review_stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="Review",
            display_order=90,
        )
        self.followup_stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="Follow Up",
            display_order=91,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Aarav",
            phone="+919999999991",
        )
        other_pipeline = Pipeline.objects.create(
            organization=self.other_organization,
            name="Other Sales",
        )
        other_stage = other_pipeline.stages.get(name="New leads")
        self.other_lead = Lead.objects.create(
            organization=self.other_organization,
            pipeline=other_pipeline,
            stage=other_stage,
            name="Other Tenant Lead",
            phone="+919999999992",
        )
        self.oauth_client = OperationsOAuthClient.objects.create(
            client_id="operations_test_client",
            client_name="Test MCP",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

    def _token(self, *, actor, role, organization=None, scopes=None):
        raw = "test-bearer"
        OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=actor,
            organization=organization,
            role=role,
            access_token_hash=token_hash(raw),
            refresh_token_hash=token_hash("test-refresh"),
            scope=" ".join(
                scopes or [OPERATIONS_READ_SCOPE, OPERATIONS_WRITE_SCOPE]
            ),
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=4),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        return raw

    def _call(self, bearer, name, arguments=None):
        return self.client.post(
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

    def _list_tools(self, bearer=None):
        headers = {}
        if bearer:
            headers["HTTP_AUTHORIZATION"] = "Bearer " + bearer
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "tools-list",
                    "method": "tools/list",
                    "params": {},
                }
            ),
            content_type="application/json",
            **headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def _result(self, response):
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def test_operations_agent_contract_is_exposed_by_mcp_discovery(self):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "discover-contract",
                    "method": "server/discover",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        }
                    },
                }
            ),
            content_type="application/json",
            HTTP_MCP_PROTOCOL_VERSION="2026-07-28",
            HTTP_MCP_METHOD="server/discover",
        )
        self.assertEqual(response.status_code, 200)
        instructions = response.json()["result"]["instructions"]
        self.assertEqual(instructions, OPERATIONS_AGENT_INSTRUCTIONS)
        for required_text in (
            "SHVYA backend permissions",
            "Your authority <= authenticated user's authority",
            "ROOT_CAUSE_CONFIRMED",
            "approved=true",
            "Never mix tenant data",
            "Never say \"fixed\" before verification",
            "DO NOT STORE PRIVATE REASONING",
            "NO RAW SYSTEM ACCESS",
        ):
            self.assertIn(required_text, instructions)

    def test_reused_diagnostic_tools_advertise_operations_oauth_scopes(self):
        result = self._list_tools()
        tools = {
            item["name"]: item
            for item in result["tools"]
        }
        for name in (
            "find_leads",
            "get_lead_snapshot",
            "get_ai_diagnostics",
            "get_runtime_health",
        ):
            self.assertIn(name, tools)
            schemes = tools[name]["securitySchemes"]
            self.assertEqual(schemes[0]["type"], "oauth2")
            self.assertIn(
                OPERATIONS_READ_SCOPE,
                schemes[0]["scopes"],
            )
            self.assertIn(
                OPERATIONS_WRITE_SCOPE,
                schemes[0]["scopes"],
            )
            self.assertNotIn(
                "diagnostics.read",
                schemes[0]["scopes"],
            )

    def test_operations_tools_advertise_configuration_surfaces(self):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        tools = {
            item["name"]: item
            for item in response.json()["result"]["tools"]
        }
        for name in (
            "upsert_pipeline_configuration",
            "upsert_stage_configuration",
            "upsert_attribute_configuration",
            "upsert_workflow_configuration",
            "upsert_cadence_configuration",
            "add_cadence_step",
        ):
            self.assertIn(name, tools)
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"])

    def test_support_context_tools_do_not_advertise_fake_dry_run_fields(self):
        tools = {
            item["name"]: item
            for item in self._list_tools()["tools"]
        }
        for name in (
            "select_organization_context",
            "clear_organization_context",
        ):
            properties = tools[name]["inputSchema"]["properties"]
            self.assertIn("reason", properties)
            self.assertNotIn("dry_run", properties)
            self.assertNotIn("approved", properties)

        mutation_properties = tools["move_lead_stage"]["inputSchema"][
            "properties"
        ]
        self.assertIn("dry_run", mutation_properties)
        self.assertIn("approved", mutation_properties)

    def test_direct_disabled_capability_call_returns_superadmin_required(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        result = self._result(
            self._call(
                bearer,
                "get_runtime_health",
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "SUPERADMIN_REQUIRED",
        )

    def test_authenticated_org_admin_tool_discovery_matches_superadmin_policy(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE, OPERATIONS_WRITE_SCOPE],
        )

        result = self._list_tools(bearer)
        names = {item["name"] for item in result["tools"]}

        self.assertIn("get_operations_context", names)
        self.assertIn("get_organization_configuration", names)
        self.assertIn("diagnose_lead_qualification", names)
        self.assertIn("find_leads", names)
        self.assertNotIn("list_organizations", names)
        self.assertNotIn("select_organization_context", names)
        self.assertNotIn("clear_organization_context", names)
        self.assertNotIn("move_lead_stage", names)
        self.assertNotIn("update_lead_attributes", names)
        self.assertNotIn("update_ai_configuration", names)
        self.assertNotIn("upsert_pipeline_configuration", names)
        self.assertNotIn("upsert_workflow_configuration", names)

    def test_authenticated_org_admin_tool_discovery_updates_when_policy_changes(self):
        policy = OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        before = {item["name"] for item in self._list_tools(bearer)["tools"]}
        self.assertNotIn("move_lead_stage", before)

        policy.allowed_capabilities = [
            CAP_ORGANIZATION_READ,
            CAP_LEAD_STAGE_WRITE,
        ]
        policy.save(update_fields=["allowed_capabilities", "updated_at"])

        after = {item["name"] for item in self._list_tools(bearer)["tools"]}
        self.assertIn("move_lead_stage", after)
        self.assertIn("repair_qualification_stage", after)

    def test_authenticated_superadmin_tool_discovery_keeps_full_operations_surface(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        result = self._list_tools(bearer)
        names = {item["name"] for item in result["tools"]}

        self.assertIn("list_organizations", names)
        self.assertIn("select_organization_context", names)
        self.assertIn("move_lead_stage", names)
        self.assertIn("update_ai_configuration", names)
        self.assertIn("upsert_workflow_configuration", names)
        self.assertIn("find_leads", names)

    def test_existing_cadence_sender_change_is_explicitly_rejected(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        first = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="First Sender",
            display_phone_number="+919000000001",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        second = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Second Sender",
            display_phone_number="+919000000002",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Existing Cadence",
            description="Test",
            whatsapp_account=first,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        result = self._result(
            self._call(
                bearer,
                "upsert_cadence_configuration",
                {
                    "cadence_id": str(sequence.id),
                    "data": {
                        "name": sequence.name,
                        "description": sequence.description,
                        "provider": "api",
                        "whatsapp_account_id": str(second.id),
                    },
                    "reason": "Attempt sender change on cadence",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        sequence.refresh_from_db()
        self.assertEqual(sequence.whatsapp_account_id, first.id)

    def test_operations_automation_writes_reject_secret_like_content(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Automation Sender",
            display_phone_number="+919000000003",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Safe Cadence",
            description="Test",
            whatsapp_account=account,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        workflow = self._result(
            self._call(
                bearer,
                "upsert_workflow_configuration",
                {
                    "data": {
                        "name": "password: workflow-secret",
                    },
                    "reason": "Attempt unsafe workflow configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(workflow["isError"])
        self.assertEqual(
            workflow["structuredContent"]["status"],
            "NOT_ALLOWED",
        )

        cadence_step = self._result(
            self._call(
                bearer,
                "add_cadence_step",
                {
                    "cadence_id": str(sequence.id),
                    "data": {
                        "type": "reminder",
                        "text": "password: cadence-secret",
                        "schedule": {"type": "immediate"},
                    },
                    "reason": "Attempt unsafe cadence content",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(cadence_step["isError"])
        self.assertEqual(
            cadence_step["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertEqual(sequence.steps.count(), 0)

    def test_operations_crm_and_lead_writes_reject_secret_like_values(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
            approval_required_capabilities=[],
        )
        context_attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Customer Context",
            key="customer_context",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        pipeline_result = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {
                    "data": {
                        "name": "Unsafe Pipeline",
                        "description": "api_key: pipeline-secret",
                    },
                    "reason": "Attempt unsafe pipeline configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(pipeline_result["isError"])
        self.assertEqual(
            pipeline_result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertFalse(
            Pipeline.objects.filter(
                organization=self.organization,
                name="Unsafe Pipeline",
            ).exists()
        )

        lead_result = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    "lead_id": str(self.lead.id),
                    "values": {
                        context_attribute.key: "password: lead-secret",
                    },
                    "reason": "Attempt unsafe lead attribute write",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(lead_result["isError"])
        self.assertEqual(
            lead_result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.lead.refresh_from_db()
        self.assertNotIn(
            context_attribute.key,
            self.lead.attributes or {},
        )

    def test_pipeline_configuration_cannot_deactivate_pipeline_with_leads(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        result = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {
                    "pipeline_id": str(self.pipeline.id),
                    "data": {"is_active": False},
                    "reason": "Attempt pipeline deactivation with live leads",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.pipeline.refresh_from_db()
        self.assertTrue(self.pipeline.is_active)

    def test_attribute_type_change_rejects_incompatible_existing_lead_values(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Legacy Context",
            key="legacy_context",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "not-a-number",
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {
                    "attribute_id": str(definition.id),
                    "data": {
                        "name": definition.name,
                        "field_type": "numeric",
                        "description": "",
                        "options": [],
                    },
                    "reason": "Attempt incompatible attribute type change",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "existing CRM values invalid",
            result["structuredContent"]["error"],
        )
        definition.refresh_from_db()
        self.assertEqual(
            definition.field_type,
            AttributeDefinition.FieldType.TEXT,
        )

        self.lead.attributes[definition.key] = "42"
        self.lead.save(update_fields=["attributes", "updated_at"])
        allowed = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {
                    "attribute_id": str(definition.id),
                    "data": {
                        "name": definition.name,
                        "field_type": "numeric",
                        "description": "",
                        "options": [],
                    },
                    "reason": "Review compatible attribute type change",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(allowed["isError"])
        self.assertEqual(
            allowed["structuredContent"]["status"],
            "DRY_RUN",
        )

    def test_cadence_step_verification_checks_schedule_and_content(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Cadence Verify Sender",
            display_phone_number="+919000000004",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Verification Cadence",
            description="Test",
            whatsapp_account=account,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "cadence_id": str(sequence.id),
            "data": {
                "type": "reminder",
                "text": "Call this lead",
                "schedule": {
                    "type": "delay",
                    "delay_value": 2,
                    "delay_unit": "hours",
                },
            },
            "reason": "Add verified reminder cadence step",
            "dry_run": False,
        }

        applied = self._result(
            self._call(
                bearer,
                "add_cadence_step",
                arguments,
            )
        )
        self.assertFalse(applied["isError"])
        self.assertEqual(
            applied["structuredContent"]["status"],
            "FIXED",
        )
        step = sequence.steps.get()
        self.assertEqual(step.step_type, FollowupStep.StepType.REMINDER)
        self.assertEqual(step.reminder_text, "Call this lead")
        self.assertEqual(step.schedule_type, FollowupStep.ScheduleType.DELAY)
        self.assertEqual(step.delay_value, 2)
        self.assertEqual(step.delay_unit, FollowupStep.DelayUnit.HOURS)

        sequence.steps.all().delete()

        def create_wrong_schedule(*, sequence, text, **kwargs):
            return FollowupStep.objects.create(
                sequence=sequence,
                position=sequence.steps.count() + 1,
                step_type=FollowupStep.StepType.REMINDER,
                title="Wrong schedule",
                reminder_text=text,
                schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
            )

        with patch(
            "apps.integrations.operations_tools.add_reminder_step",
            side_effect=create_wrong_schedule,
        ):
            failed = self._result(
                self._call(
                    bearer,
                    "add_cadence_step",
                    {
                        **arguments,
                        "reason": "Detect mismatched cadence persistence",
                    },
                )
            )
        self.assertTrue(failed["isError"])
        self.assertEqual(
            failed["structuredContent"]["status"],
            "FAILED",
        )
        self.assertIn(
            "schedule_type",
            failed["structuredContent"]["error"],
        )

    def test_pipeline_configuration_create_verifies_standard_stages(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {
                    "data": {
                        "name": "Enterprise Sales",
                        "description": "Enterprise pipeline",
                        "is_active": True,
                        "ai_enabled": True,
                    },
                    "reason": "Create enterprise sales pipeline",
                    "dry_run": False,
                },
            )
        )
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "FIXED",
        )
        pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Enterprise Sales",
        )
        active_names = set(
            pipeline.stages.filter(is_active=True).values_list(
                "name",
                flat=True,
            )
        )
        self.assertTrue(
            {
                "New leads",
                "Qualified",
                "Nurturing",
                "Average lead",
                "Ultra Hot",
                "Lead Won",
                "DNP",
                "Lead Lost",
            }.issubset(active_names)
        )

    def test_stage_configuration_cannot_deactivate_occupied_stage(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        move_lead_to_stage(
            lead=self.lead,
            stage=self.review_stage,
            actor=self.admin,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "upsert_stage_configuration",
                {
                    "pipeline_id": str(self.pipeline.id),
                    "stage_id": str(self.review_stage.id),
                    "data": {"is_active": False},
                    "reason": "Attempt occupied stage deactivation",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.review_stage.refresh_from_db()
        self.assertTrue(self.review_stage.is_active)

    def test_attribute_configuration_uses_policy_dry_run_approval_and_service(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[CAP_CRM_CONFIG_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "data": {
                "name": "Company Size",
                "field_type": "numeric",
                "description": "Approximate employee count",
            },
            "reason": "Add qualification CRM attribute",
        }

        dry = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])
        self.assertEqual(dry["structuredContent"]["status"], "DRY_RUN")
        self.assertFalse(
            AttributeDefinition.objects.filter(
                organization=self.organization,
                name="Company Size",
            ).exists()
        )

        blocked = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {**arguments, "dry_run": False},
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        applied = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertFalse(applied["isError"])
        self.assertEqual(applied["structuredContent"]["status"], "FIXED")
        approval_use = OperationsApprovalUse.objects.get(
            approval_event_id=dry["structuredContent"]["approval_event_id"]
        )
        with self.assertRaises(ValidationError):
            OperationsApprovalUse.objects.filter(
                pk=approval_use.pk
            ).update(created_at=timezone.now())
        with self.assertRaises(ValidationError):
            OperationsApprovalUse.objects.filter(
                pk=approval_use.pk
            ).delete()

        replacement_use = OperationsApprovalUse(
            id=approval_use.id,
            approval_event=approval_use.approval_event,
        )
        with self.assertRaises(ValidationError):
            OperationsApprovalUse.objects.bulk_create(
                [replacement_use],
                update_conflicts=True,
                update_fields=["created_at"],
                unique_fields=["id"],
            )
        definition = AttributeDefinition.objects.get(
            organization=self.organization,
            name="Company Size",
        )
        self.assertEqual(definition.key, "company_size")
        self.assertEqual(definition.field_type, "numeric")

    def test_operations_reads_redact_sensitive_attributes_workflows_and_playbook_secrets(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        sensitive = AttributeDefinition.objects.create(
            organization=self.organization,
            name="API Key",
            key="api_key",
            field_type=AttributeDefinition.FieldType.TEXT,
            description="Credential-like field that must stay private.",
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_playbook = (
            "Use this safe business rule. password: secret-pass-value "
            "Never disclose it."
        )
        info.save(update_fields=["ai_playbook"])

        SmartTrigger.objects.create(
            organization=self.organization,
            name="Sensitive legacy workflow",
            enabled=True,
            position=1,
            trigger_type="keyword",
            conditions={
                "scopes": [],
                "attributes": [
                    {
                        "key": sensitive.key,
                        "match": "equals",
                        "values": ["workflow-secret-value"],
                    }
                ],
            },
            action_type="attribute",
            action={
                "key": sensitive.key,
                "value": "workflow-action-secret",
            },
            fingerprint="f" * 64,
            created_by=self.admin,
        )

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        config = self._result(
            self._call(bearer, "get_organization_configuration")
        )
        self.assertFalse(config["isError"])
        config_json = json.dumps(config["structuredContent"])
        self.assertNotIn('"api_key"', config_json)
        self.assertNotIn("secret-pass-value", config_json)
        self.assertIn("[REDACTED]", config_json)

        automation = self._result(
            self._call(bearer, "get_automation_configuration")
        )
        self.assertFalse(automation["isError"])
        automation_json = json.dumps(automation["structuredContent"])
        self.assertNotIn("workflow-secret-value", automation_json)
        self.assertNotIn("workflow-action-secret", automation_json)
        self.assertNotIn('"api_key"', automation_json)
        self.assertIn("[REDACTED_SENSITIVE_ATTRIBUTE]", automation_json)
        self.assertGreaterEqual(
            automation["structuredContent"]["counts"][
                "sensitive_workflow_fields_redacted"
            ],
            2,
        )

    def test_conversion_analysis_uses_same_created_lead_cohort_for_rate(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        old_lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Old Lead",
            phone="+919999999993",
        )
        old_created_at = timezone.now() - timedelta(days=45)
        Lead.objects.filter(pk=old_lead.pk).update(
            created_at=old_created_at,
        )
        old_lead.refresh_from_db()
        move_lead_to_stage(
            lead=old_lead,
            stage=self.qualified,
            actor=self.admin,
        )

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        result = self._result(
            self._call(
                bearer,
                "get_conversion_analysis",
                {"days": 30},
            )
        )
        self.assertFalse(result["isError"])
        current = result["structuredContent"]["comparison"][
            "current_period"
        ]
        self.assertEqual(current["lead_volume"], 1)
        self.assertEqual(current["qualified_transitions"], 0)
        self.assertEqual(
            current["qualified_transition_rate"],
            0.0,
        )

    def test_read_context_does_not_create_missing_policy_row(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        self.assertFalse(
            OperationsPolicy.objects.filter(
                organization=self.organization,
            ).exists()
        )
        result = self._result(
            self._call(bearer, "list_organizations")
        )
        self.assertFalse(result["isError"])
        self.assertFalse(
            OperationsPolicy.objects.filter(
                organization=self.organization,
            ).exists()
        )

    def test_operations_ai_configuration_rejects_secret_like_content(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        blocked = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "ai_playbook": (
                            "Qualification rules. password: super-secret-value"
                        ),
                    },
                    "reason": "Attempt unsafe AI configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )

        allowed = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "ai_playbook": (
                            "Ask only the configured qualification questions "
                            "and never request credentials."
                        ),
                    },
                    "reason": "Review safe AI configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(allowed["isError"])
        self.assertEqual(
            allowed["structuredContent"]["status"],
            "DRY_RUN",
        )

    def test_operations_ai_configuration_can_create_first_org_info_and_verify(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )

        applied = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "about": "Test organization business profile",
                        "ai_playbook": (
                            "Answer business questions from approved context and "
                            "use backend qualification rules."
                        ),
                    },
                    "reason": "Create initial AI configuration",
                    "dry_run": False,
                },
            )
        )
        self.assertFalse(applied["isError"])
        self.assertEqual(
            applied["structuredContent"]["status"],
            "FIXED",
        )
        self.assertEqual(
            applied["structuredContent"]["verification"],
            "passed",
        )

        info = OrgInfo.objects.get(organization=self.organization)
        self.assertEqual(
            info.about,
            "Test organization business profile",
        )
        self.assertIn(
            "backend qualification rules",
            info.ai_playbook,
        )

    def test_ai_configuration_dry_run_does_not_create_org_info(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Prepare AI configuration review",
                },
            )
        )
        self.assertFalse(selected["isError"])
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )

        dry = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "about": "Test organization context",
                    },
                    "reason": "Review proposed AI context change",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(dry["isError"])
        self.assertEqual(dry["structuredContent"]["status"], "DRY_RUN")
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )

    def test_oauth_consent_shows_requested_scope_and_effective_capabilities(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[CAP_LEAD_STAGE_WRITE],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )

        verifier = "c" * 64
        response = self.client.get(
            "/operations/oauth/authorize",
            {
                "client_id": self.oauth_client.client_id,
                "redirect_uri": "https://chatgpt.com/aip/callback",
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": (
                    f"{OPERATIONS_READ_SCOPE} "
                    f"{OPERATIONS_WRITE_SCOPE}"
                ),
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("REQUESTED ACCESS", body)
        self.assertIn("Read SHVYA Operations", body)
        self.assertIn("Request write capability", body)
        self.assertIn("Organization Admin", body)
        self.assertIn(self.organization.name, body)
        self.assertIn("Read business &amp; CRM configuration", body)
        self.assertIn("Move leads between active stages/pipelines", body)
        self.assertIn("approval", body)
        self.assertIn("does not disclose your password", body)

    def test_oauth_metadata_advertises_revocation_endpoint(self):
        metadata = self.client.get(
            "/operations/.well-known/oauth-authorization-server"
        )
        self.assertEqual(metadata.status_code, 200)
        body = metadata.json()
        self.assertEqual(
            body["revocation_endpoint"],
            "http://testserver/operations/oauth/revoke",
        )
        self.assertEqual(
            body["revocation_endpoint_auth_methods_supported"],
            ["none"],
        )

    def test_registration_accepts_chatgpt_claude_and_exact_vscode_callbacks(self):
        for callback in (
            "https://chatgpt.com/aip/callback",
            "https://claude.ai/api/mcp/auth_callback",
            "http://127.0.0.1:33418",
            "https://vscode.dev/redirect",
        ):
            response = self.client.post(
                "/operations/oauth/register",
                data=json.dumps(
                    {
                        "client_name": "External AI",
                        "redirect_uris": [callback],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 201)

        rejected = self.client.post(
            "/operations/oauth/register",
            data=json.dumps(
                {
                    "redirect_uris": ["https://example.com/callback"],
                    "token_endpoint_auth_method": "none",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 400)

        for unsafe_callback in (
            "http://127.0.0.1:33419",
            "http://localhost:33418",
            "https://vscode.dev/not-the-mcp-redirect",
        ):
            rejected = self.client.post(
                "/operations/oauth/register",
                data=json.dumps(
                    {
                        "redirect_uris": [unsafe_callback],
                        "token_endpoint_auth_method": "none",
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(rejected.status_code, 400)

    def test_oauth_binds_org_admin_identity_and_strips_ungranted_write_scope(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

        verifier = "v" * 64
        callback = "https://chatgpt.com/aip/callback"
        resource = "http://testserver/operations/mcp/"
        authorize = self.client.post(
            "/operations/oauth/authorize",
            data={
                "client_id": self.oauth_client.client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": f"{OPERATIONS_READ_SCOPE} {OPERATIONS_WRITE_SCOPE}",
                "resource": resource,
                "actor_mode": ROLE_ORGANIZATION_ADMIN,
            },
        )
        self.assertEqual(authorize.status_code, 302)
        code = parse_qs(urlparse(authorize["Location"]).query)["code"][0]

        token_response = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.oauth_client.client_id,
                "code": code,
                "redirect_uri": callback,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        self.assertEqual(token_response.status_code, 200)
        body = token_response.json()
        self.assertIn(OPERATIONS_READ_SCOPE, body["scope"].split())
        self.assertNotIn(OPERATIONS_WRITE_SCOPE, body["scope"].split())

        token = OperationsOAuthToken.objects.get()
        self.assertEqual(token.actor_id, self.admin.id)
        self.assertEqual(token.organization_id, self.organization.id)
        self.assertEqual(token.role, ROLE_ORGANIZATION_ADMIN)

    def test_superadmin_policy_ignores_approval_flags_on_read_capabilities(self):
        superadmin_session = SessionStore()
        set_authenticated_user(superadmin_session, self.superadmin)
        superadmin_session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = (
            superadmin_session.session_key
        )

        response = self.client.post(
            reverse(
                "superadmin-organization-operations-mcp-policy",
                kwargs={"organization_id": self.organization.id},
            ),
            {
                "organization_admin_enabled": "on",
                "allowed_capabilities": [
                    CAP_ORGANIZATION_READ,
                    CAP_DIAGNOSTICS_READ,
                    CAP_LEAD_STAGE_WRITE,
                ],
                "approval_required_capabilities": [
                    CAP_ORGANIZATION_READ,
                    CAP_DIAGNOSTICS_READ,
                    CAP_LEAD_STAGE_WRITE,
                ],
            },
        )
        self.assertEqual(response.status_code, 302)

        policy = OperationsPolicy.objects.get(
            organization=self.organization
        )
        self.assertEqual(
            set(policy.approval_required_capabilities),
            {CAP_LEAD_STAGE_WRITE},
        )

    def test_superadmin_disable_revokes_existing_org_admin_tokens_permanently(self):
        policy = OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        superadmin_session = SessionStore()
        set_authenticated_user(superadmin_session, self.superadmin)
        superadmin_session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = (
            superadmin_session.session_key
        )

        disable = self.client.post(
            reverse(
                "superadmin-organization-operations-mcp-policy",
                kwargs={"organization_id": self.organization.id},
            ),
            {
                "allowed_capabilities": [CAP_ORGANIZATION_READ],
            },
        )
        self.assertEqual(disable.status_code, 302)

        token = OperationsOAuthToken.objects.get(actor=self.admin)
        self.assertIsNotNone(token.revoked_at)
        policy.refresh_from_db()
        self.assertFalse(policy.organization_admin_enabled)

        enable = self.client.post(
            reverse(
                "superadmin-organization-operations-mcp-policy",
                kwargs={"organization_id": self.organization.id},
            ),
            {
                "organization_admin_enabled": "on",
                "allowed_capabilities": [CAP_ORGANIZATION_READ],
            },
        )
        self.assertEqual(enable.status_code, 302)
        policy.refresh_from_db()
        self.assertTrue(policy.organization_admin_enabled)

        old_token_result = self._result(
            self._call(bearer, "get_operations_context")
        )
        self.assertTrue(old_token_result["isError"])
        self.assertIn("mcp/www_authenticate", old_token_result["_meta"])

    def test_oauth_revocation_invalidates_access_and_closes_support_session(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Validate OAuth revocation behavior",
                },
            )
        )
        self.assertFalse(selected["isError"])
        session = OperationsSupportSession.objects.get(ended_at__isnull=True)

        revoke = self.client.post(
            reverse("shvya-operations-oauth-revoke"),
            {"token": bearer, "token_type_hint": "access_token"},
        )
        self.assertEqual(revoke.status_code, 200)

        token = OperationsOAuthToken.objects.get(actor=self.superadmin)
        token.refresh_from_db()
        session.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        self.assertIsNotNone(session.ended_at)
        audit = OperationsAuditEvent.objects.get(
            actor=self.superadmin,
            tool_name="oauth_revoke",
        )
        self.assertEqual(audit.organization_id, self.organization.id)
        self.assertEqual(audit.support_session_id, session.id)
        self.assertEqual(audit.target_type, "oauth_grant")
        self.assertNotIn(bearer, json.dumps(audit.change_summary))
        self.assertNotIn(bearer, audit.request_fingerprint)

        result = self._result(
            self._call(bearer, "get_operations_context")
        )
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])

    def test_oauth_revocation_by_refresh_token_invalidates_access_grant(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        revoke = self.client.post(
            reverse("shvya-operations-oauth-revoke"),
            {"token": "test-refresh", "token_type_hint": "refresh_token"},
        )
        self.assertEqual(revoke.status_code, 200)

        token = OperationsOAuthToken.objects.get(actor=self.admin)
        self.assertIsNotNone(token.revoked_at)

        result = self._result(
            self._call(bearer, "get_operations_context")
        )
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])

    def test_oauth_revocation_does_not_disclose_unknown_token_state(self):
        response = self.client.post(
            reverse("shvya-operations-oauth-revoke"),
            {"token": "unknown-token-value"},
        )
        self.assertEqual(response.status_code, 200)

    def test_org_admin_token_is_rejected_while_policy_disabled(self):
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        result = self._result(self._call(bearer, "get_operations_context"))
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])

    def test_org_admin_is_tenant_scoped_and_cannot_switch_context(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        own = self._result(
            self._call(
                bearer,
                "get_lead_snapshot",
                {"lead_id": str(self.lead.id)},
            )
        )
        self.assertFalse(own["isError"])
        self.assertEqual(own["structuredContent"]["id"], str(self.lead.id))

        foreign = self._result(
            self._call(
                bearer,
                "get_lead_snapshot",
                {"lead_id": str(self.other_lead.id)},
            )
        )
        self.assertTrue(foreign["isError"])
        self.assertNotIn(self.other_lead.name, json.dumps(foreign))

        switch = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.other_organization.id),
                    "reason": "Attempt tenant switch",
                },
            )
        )
        self.assertTrue(switch["isError"])
        self.assertEqual(switch["structuredContent"]["status"], "NOT_ALLOWED")

    def test_superadmin_customer_tools_fail_closed_if_selected_org_is_disabled(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Investigate organization before disable",
                },
            )
        )
        self.assertFalse(selected["isError"])

        self.organization.is_active = False
        self.organization.save(update_fields=["is_active", "updated_at"])

        blocked = self._result(
            self._call(
                bearer,
                "get_organization_configuration",
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "organization is disabled",
            blocked["structuredContent"]["error"].lower(),
        )

        context = self._result(
            self._call(bearer, "get_operations_context")
        )
        self.assertFalse(context["isError"])
        self.assertFalse(
            context["structuredContent"]["organization"]["active"]
        )

        support_session = OperationsSupportSession.objects.get(
            token__actor=self.superadmin,
            organization=self.organization,
            ended_at__isnull=True,
        )
        cleared = self._result(
            self._call(
                bearer,
                "clear_organization_context",
                {"reason": "Leave disabled organization context"},
            )
        )
        self.assertFalse(cleared["isError"])
        self.assertEqual(
            cleared["structuredContent"]["status"],
            "cleared",
        )
        support_session.refresh_from_db()
        self.assertIsNotNone(support_session.ended_at)

        audit = OperationsAuditEvent.objects.get(
            actor=self.superadmin,
            tool_name="clear_organization_context",
        )
        self.assertEqual(
            audit.organization_id,
            self.organization.id,
        )
        self.assertEqual(
            audit.support_session_id,
            support_session.id,
        )

    def test_superadmin_requires_explicit_context_and_creates_support_session(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        before = self._result(
            self._call(bearer, "get_organization_configuration")
        )
        self.assertTrue(before["isError"])
        self.assertIn(
            "Select an organization support context",
            before["structuredContent"]["error"],
        )

        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Investigate qualification issue",
                },
            )
        )
        self.assertFalse(selected["isError"])
        self.assertEqual(
            selected["structuredContent"]["organization"]["id"],
            str(self.organization.id),
        )
        session = OperationsSupportSession.objects.get(ended_at__isnull=True)
        self.assertEqual(session.organization_id, self.organization.id)
        self.assertEqual(session.actor_id, self.superadmin.id)

        configured = self._result(
            self._call(bearer, "get_organization_configuration")
        )
        self.assertFalse(configured["isError"])
        self.assertEqual(
            configured["structuredContent"]["organization"]["id"],
            str(self.organization.id),
        )

    def test_write_policy_enforces_dry_run_approval_execute_and_verify(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[CAP_LEAD_STAGE_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        base_arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Move lead to reviewed sales stage",
        }

        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**base_arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])
        self.assertEqual(dry["structuredContent"]["status"], "DRY_RUN")
        approval_event_id = dry["structuredContent"]["approval_event_id"]
        self.assertIn(
            approval_event_id,
            dry["content"][0]["text"],
        )
        self.assertEqual(
            dry["structuredContent"]["approval_expires_in_seconds"],
            1800,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

        blocked = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**base_arguments, "dry_run": False},
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        wrong_receipt = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **base_arguments,
                    "target_stage_id": str(self.new_stage.id),
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(wrong_receipt["isError"])
        self.assertEqual(
            wrong_receipt["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        applied = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **base_arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertFalse(applied["isError"])
        self.assertEqual(applied["structuredContent"]["status"], "FIXED")
        self.assertEqual(
            applied["structuredContent"]["verification"],
            "passed",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.review_stage.id)
        self.assertTrue(
            LeadActivity.objects.filter(
                lead=self.lead,
                organization=self.organization,
                topic=LeadActivity.Topic.STAGE_CHANGED,
                new_stage=self.review_stage,
            ).exists()
        )

        replayed_approval = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **base_arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(replayed_approval["isError"])
        self.assertEqual(
            replayed_approval["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        outcomes = list(
            OperationsAuditEvent.objects.filter(
                organization=self.organization,
                tool_name="move_lead_stage",
            ).values_list("outcome", flat=True)
        )
        self.assertIn(OperationsAuditEvent.Outcome.DRY_RUN, outcomes)
        self.assertIn(
            OperationsAuditEvent.Outcome.APPROVAL_REQUIRED,
            outcomes,
        )
        self.assertIn(OperationsAuditEvent.Outcome.SUCCESS, outcomes)

    def test_operations_approval_receipt_expires(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[CAP_LEAD_STAGE_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Review time-bounded stage change",
        }
        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])
        event = OperationsAuditEvent.objects.get(
            pk=dry["structuredContent"]["approval_event_id"]
        )

        future = event.created_at + timedelta(minutes=31)
        with patch(
            "apps.integrations.operations_tools.timezone.now",
            return_value=future,
        ):
            expired = self._result(
                self._call(
                    bearer,
                    "move_lead_stage",
                    {
                        **arguments,
                        "dry_run": False,
                        "approved": True,
                        "approval_event_id": str(event.id),
                    },
                )
            )
        self.assertTrue(expired["isError"])
        self.assertEqual(
            expired["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_qualification_diagnosis_completed_but_not_qualified_is_not_failure(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        snapshot = (
            {"mode": "guided", "flow_version": "test-flow"},
            [{"id": "budget", "question": "What is your budget?", "required": True}],
            {
                "qualification_status": "completed",
                "qualification_result": "",
                "all_requirements_answered": True,
                "answered_requirement_ids": ["budget"],
                "missing_requirement_ids": [],
                "flow_version": "test-flow",
            },
            {"errors": []},
            {
                "qualified": False,
                "reason": "criteria_evaluated",
                "rules": [{"verdict": "fail"}],
            },
            None,
        )

        with patch(
            "apps.integrations.operations_tools._qualification_contract_snapshot",
            return_value=snapshot,
        ):
            result = self._result(
                self._call(
                    bearer,
                    "diagnose_lead_qualification",
                    {"lead_id": str(self.lead.id)},
                )
            )

        self.assertFalse(result["isError"])
        diagnosis = result["structuredContent"]
        self.assertEqual(
            diagnosis["classification"],
            "NO_PROBLEM_FOUND",
        )
        self.assertFalse(diagnosis["repair_available"])
        self.assertFalse(
            diagnosis["qualification"]["criteria_qualified"]
        )
        self.assertIn(
            "transition is not expected",
            diagnosis["root_cause"],
        )

    def test_qualification_repair_supports_authoritative_cross_pipeline_target(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[],
        )
        target_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Qualified Pipeline",
        )
        target_stage = target_pipeline.stages.get(name="Qualified")
        target = {
            "id": str(target_stage.id),
            "name": target_stage.name,
            "pipeline_id": str(target_pipeline.id),
            "pipeline__name": target_pipeline.name,
        }
        snapshot = (
            {"mode": "guided", "flow_version": "repair-flow"},
            [],
            {
                "qualification_status": "completed",
                "qualification_result": "qualified",
                "all_requirements_answered": True,
                "answered_requirement_ids": [],
                "missing_requirement_ids": [],
                "flow_version": "repair-flow",
            },
            {"errors": [], "completion_stage": target},
            {
                "qualified": True,
                "reason": "criteria_evaluated",
                "rules": [],
            },
            target,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        with patch(
            "apps.integrations.operations_tools._qualification_contract_snapshot",
            return_value=snapshot,
        ):
            dry = self._result(
                self._call(
                    bearer,
                    "repair_qualification_stage",
                    {
                        "lead_id": str(self.lead.id),
                        "reason": "Repair verified qualification completion",
                        "dry_run": True,
                    },
                )
            )
            self.assertFalse(dry["isError"])
            self.assertEqual(
                dry["structuredContent"]["status"],
                "DRY_RUN",
            )
            proposed = dry["structuredContent"]["proposed_change"]
            self.assertEqual(
                proposed["after"]["pipeline_id"],
                str(target_pipeline.id),
            )
            self.assertEqual(
                proposed["after"]["stage_id"],
                str(target_stage.id),
            )

            applied = self._result(
                self._call(
                    bearer,
                    "repair_qualification_stage",
                    {
                        "lead_id": str(self.lead.id),
                        "reason": "Repair verified qualification completion",
                        "dry_run": False,
                    },
                )
            )

        self.assertFalse(applied["isError"])
        self.assertEqual(
            applied["structuredContent"]["status"],
            "FIXED",
        )
        self.assertEqual(
            applied["structuredContent"]["verification"],
            "passed",
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.pipeline_id,
            target_pipeline.id,
        )
        self.assertEqual(
            self.lead.stage_id,
            target_stage.id,
        )

    def test_lead_attribute_approval_is_invalid_after_values_change(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
            approval_required_capabilities=[CAP_LEAD_ATTRIBUTES_WRITE],
        )
        definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Company Size",
            key="company_size",
            field_type=AttributeDefinition.FieldType.NUMERIC,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "values": {definition.key: "25"},
            "reason": "Update reviewed company size",
        }
        dry = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "40",
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

        stale = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(stale["isError"])
        self.assertEqual(
            stale["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.attributes[definition.key],
            "40",
        )

    def test_pipeline_config_approval_is_invalid_after_pipeline_changes(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[CAP_CRM_CONFIG_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "pipeline_id": str(self.pipeline.id),
            "data": {
                "name": self.pipeline.name,
                "description": "Approved description",
                "is_active": True,
                "ai_enabled": True,
            },
            "reason": "Update reviewed pipeline description",
        }
        dry = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        self.pipeline.description = "Newer manual description"
        self.pipeline.save(update_fields=["description", "updated_at"])

        stale = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(stale["isError"])
        self.assertEqual(
            stale["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.pipeline.refresh_from_db()
        self.assertEqual(
            self.pipeline.description,
            "Newer manual description",
        )

    def test_operations_stage_move_cannot_bypass_backend_qualification(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        result = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.qualified.id),
                    "reason": "Attempt direct Qualified transition",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_sensitive_target_requirement_returns_manual_fix_required(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[],
        )
        sensitive = AttributeDefinition.objects.create(
            organization=self.organization,
            name="API Token",
            key="api_token",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.review_stage.config = {
            "required_attribute_ids": [str(sensitive.id)],
        }
        self.review_stage.save(update_fields=["config", "updated_at"])

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "Attempt stage with sensitive requirement",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "MANUAL_FIX_REQUIRED",
        )
        self.assertNotIn(
            "API Token",
            result["structuredContent"]["error"],
        )
        self.assertIn(
            "sensitive required CRM data",
            result["structuredContent"]["error"],
        )

    def test_operations_stage_move_enforces_target_required_attributes(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[],
        )
        required = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Company Size",
            key="company_size",
            field_type=AttributeDefinition.FieldType.NUMERIC,
        )
        self.review_stage.config = {
            "required_attribute_ids": [str(required.id)],
        }
        self.review_stage.save(update_fields=["config", "updated_at"])

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Move after required CRM data is complete",
            "dry_run": True,
        }

        blocked = self._result(
            self._call(bearer, "move_lead_stage", arguments)
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "Company Size",
            blocked["structuredContent"]["error"],
        )

        self.lead.attributes = {
            **(self.lead.attributes or {}),
            "company_size": "25",
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

        allowed = self._result(
            self._call(bearer, "move_lead_stage", arguments)
        )
        self.assertFalse(allowed["isError"])
        self.assertEqual(
            allowed["structuredContent"]["status"],
            "DRY_RUN",
        )

    def test_operations_stage_approval_is_invalid_after_lead_state_changes(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[CAP_LEAD_STAGE_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Approve move from current lead stage",
        }
        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        move_lead_to_stage(
            lead=self.lead,
            stage=self.followup_stage,
            actor=self.admin,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.followup_stage.id)

        stale = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(stale["isError"])
        self.assertEqual(
            stale["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.followup_stage.id)

    def test_approved_true_cannot_bypass_disabled_write_capability(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.qualified.id),
                    "reason": "Unauthorized stage change request",
                    "dry_run": False,
                    "approved": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "SUPERADMIN_REQUIRED",
        )
        self.assertIn(
            "SHVYA Superadmin",
            result["structuredContent"]["error"],
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_operations_audit_events_are_append_only_even_through_queryset(self):
        event = OperationsAuditEvent.objects.create(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            organization=self.organization,
            tool_name="test_tool",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(self.organization.id),
            reason="Verify immutable audit storage",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="a" * 64,
            change_summary={"status": "verified"},
        )

        event.reason = "Attempted rewrite"
        with self.assertRaises(ValidationError):
            event.save(update_fields=["reason"])

        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.filter(pk=event.pk).update(
                reason="Bulk rewrite"
            )

        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.filter(pk=event.pk).delete()

        event.reason = "Bulk update attempt"
        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.bulk_update(
                [event],
                ["reason"],
            )

        conflict = OperationsAuditEvent(
            id=event.id,
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            organization=self.organization,
            tool_name="replacement",
            outcome=OperationsAuditEvent.Outcome.ERROR,
            request_fingerprint="b" * 64,
        )
        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.bulk_create(
                [conflict],
                update_conflicts=True,
                update_fields=["tool_name", "outcome"],
                unique_fields=["id"],
            )

        event.refresh_from_db()
        self.assertEqual(event.reason, "Verify immutable audit storage")
        self.assertEqual(event.tool_name, "test_tool")

    def test_audit_stores_argument_fingerprint_not_raw_query(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        query = "+919999999991"
        result = self._result(
            self._call(bearer, "find_leads", {"query": query})
        )
        self.assertFalse(result["isError"])

        event = OperationsAuditEvent.objects.get(
            organization=self.organization,
            tool_name="find_leads",
        )
        self.assertEqual(len(event.request_fingerprint), 64)
        self.assertNotIn(query, event.request_fingerprint)
        self.assertNotIn(query, json.dumps(event.change_summary))
