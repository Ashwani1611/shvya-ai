import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.ai_engagement.models import OrgInfo
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, Pipeline
from apps.integrations.operations_agent_prompt import OPERATIONS_AGENT_INSTRUCTIONS
from apps.integrations.models import (
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
    CAP_CRM_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_STAGE_WRITE,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
)
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger


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
                },
            )
        )
        self.assertFalse(applied["isError"])
        self.assertEqual(applied["structuredContent"]["status"], "FIXED")
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
            "target_stage_id": str(self.qualified.id),
            "reason": "Qualification completion verified",
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

        applied = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **base_arguments,
                    "dry_run": False,
                    "approved": True,
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
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        self.assertTrue(
            LeadActivity.objects.filter(
                lead=self.lead,
                organization=self.organization,
                topic=LeadActivity.Topic.STAGE_CHANGED,
                new_stage=self.qualified,
            ).exists()
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
        self.assertEqual(result["structuredContent"]["status"], "NOT_ALLOWED")
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

        event.refresh_from_db()
        self.assertEqual(event.reason, "Verify immutable audit storage")

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
