import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import Lead, LeadActivity, Pipeline, Stage
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
    token_hash,
)
from apps.integrations.operations_policy import (
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_STAGE_WRITE,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
)
from apps.organizations.models import Organization


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
        self.new_stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            display_order=0,
        )
        self.qualified = Stage.objects.create(
            pipeline=self.pipeline,
            name="Qualified",
            display_order=10,
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
        other_stage = Stage.objects.create(
            pipeline=other_pipeline,
            name="New Lead",
            display_order=0,
        )
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

    def _result(self, response):
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def test_registration_accepts_chatgpt_and_claude_callbacks(self):
        for callback in (
            "https://chatgpt.com/aip/callback",
            "https://claude.ai/api/mcp/auth_callback",
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
