import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_engagement.models import OrgInfo
from apps.crm.models import Lead, Pipeline
from apps.integrations.models import (
    EmailConfiguration,
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsPolicy,
    WebhookConfiguration,
)
from apps.integrations.operations_auth import (
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    token_hash,
)
from apps.integrations.operations_policy import (
    CAP_DIAGNOSTICS_READ,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    capabilities_for_grant,
)
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerEvent, TriggerRun


class OperationsAdditionalDiagnosticsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Diagnostics Org A")
        self.target = Organization.objects.create(name="Diagnostics Org B")
        self.admin = User.objects.create_user(
            email="diagnostics-admin@example.test",
            organization=self.organization,
            password=None,
            name="Diagnostics Admin",
            role=User.Role.ADMIN,
        )
        self.superadmin = User.objects.create_superuser(
            email="diagnostics-superadmin@example.test",
            password=None,
            name="Diagnostics Support",
        )
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
            approval_required_capabilities=[],
            updated_by=self.admin,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Diagnostics Sales",
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Diagnostic Lead",
            phone="+919700000001",
        )
        self.oauth_client = OperationsOAuthClient.objects.create(
            client_id="additional_diagnostics_test_client",
            client_name="Additional Diagnostics Test",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

    def _token(self, *, actor, role, organization=None, raw):
        scopes = [OPERATIONS_READ_SCOPE, OPERATIONS_WRITE_SCOPE]
        OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=actor,
            organization=organization,
            role=role,
            access_token_hash=token_hash(raw),
            refresh_token_hash=token_hash(raw + "-refresh"),
            scope=" ".join(scopes),
            granted_capabilities=sorted(
                capabilities_for_grant(
                    role=role,
                    organization=organization,
                    allow_writes=True,
                )
            ),
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
            HTTP_X_REAL_IP="198.51.100.119",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def _ok(self, bearer, name, arguments=None):
        result = self._call(bearer, name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _select(self, bearer, organization):
        return self._ok(
            bearer,
            "select_organization_context",
            {
                "organization_id": str(organization.id),
                "reason": "Select organization for bounded diagnostic verification.",
            },
        )

    def test_new_diagnostic_tools_are_discoverable(self):
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            raw="diagnostic-discovery",
        )
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
            HTTP_AUTHORIZATION="Bearer " + bearer,
            HTTP_X_REAL_IP="198.51.100.119",
        )
        tools = {item["name"]: item for item in response.json()["result"]["tools"]}
        for name in (
            "test_integration_connection",
            "compare_organization_configuration",
            "get_configuration_integrity_diagnostics",
            "test_ai_response_policy",
        ):
            self.assertIn(name, tools)
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])

    def test_cross_organization_drift_is_superadmin_only_and_returns_no_raw_target_config(self):
        Pipeline.objects.create(
            organization=self.target,
            name="Private Target Pipeline Name",
        )
        org_bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            raw="diagnostic-org-drift",
        )
        blocked = self._call(
            org_bearer,
            "compare_organization_configuration",
            {"target_organization_id": str(self.target.id)},
        )
        self.assertTrue(blocked["isError"])

        super_bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            raw="diagnostic-super-drift",
        )
        self._select(super_bearer, self.organization)
        drift = self._ok(
            super_bearer,
            "compare_organization_configuration",
            {"target_organization_id": str(self.target.id)},
        )
        self.assertFalse(drift["raw_configuration_exposed"])
        self.assertEqual(drift["customer_records_compared"], 0)
        self.assertFalse(drift["sensitive_material_compared"])
        self.assertFalse(drift["same_configuration"])
        serialized = json.dumps(drift)
        self.assertNotIn("Private Target Pipeline Name", serialized)
        self.assertNotIn(self.target.name, serialized)
        self.assertTrue(
            all(
                len(section["current_digest"]) == 20
                and len(section["target_digest"]) == 20
                for section in drift["sections"]
            )
        )

    def test_email_live_test_reuses_no_send_canonical_connection_test(self):
        configuration = EmailConfiguration.objects.create(
            organization=self.organization,
            provider=EmailConfiguration.Provider.GMAIL,
            email_address="sales@example.test",
            smtp_host="smtp.example.test",
            smtp_port=587,
            smtp_security=EmailConfiguration.Security.STARTTLS,
            smtp_username="sales@example.test",
        )
        configuration.set_password("test-app-password")
        configuration.save(update_fields=["encrypted_password", "updated_at"])
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            raw="diagnostic-email",
        )
        with patch(
            "apps.integrations.services.email.test_email_configuration"
        ) as test_connection:
            data = self._ok(
                bearer,
                "test_integration_connection",
                {"integration": "email", "live": True},
            )
        test_connection.assert_called_once_with(configuration)
        self.assertTrue(data["ready"])
        self.assertEqual(data["live_test"], "passed")
        self.assertEqual(data["messages_sent"], 0)
        self.assertFalse(data["sensitive_values_returned"])

    def test_webhook_test_validates_target_without_sending_test_payload(self):
        webhook = WebhookConfiguration.objects.create(
            organization=self.organization,
            endpoint_url="https://example.test/shvya-hook",
            is_enabled=True,
        )
        webhook.set_secret("test-webhook-signing-secret")
        webhook.save(update_fields=["encrypted_secret", "updated_at"])
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            raw="diagnostic-webhook",
        )
        with patch(
            "apps.integrations.services.webhook.assert_public_webhook_target"
        ) as validate_target:
            data = self._ok(
                bearer,
                "test_integration_connection",
                {"integration": "webhook"},
            )
        validate_target.assert_called_once_with(webhook.endpoint_url)
        self.assertTrue(data["ready"])
        self.assertEqual(data["webhook_requests_sent"], 0)
        self.assertEqual(data["live_test"], "target_validation_only")

    def test_integrity_diagnostics_find_active_stage_in_inactive_pipeline(self):
        orphan_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Archived Parent",
        )
        orphan_pipeline.is_active = False
        orphan_pipeline.save(update_fields=["is_active", "updated_at"])
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            raw="diagnostic-integrity",
        )
        data = self._ok(
            bearer,
            "get_configuration_integrity_diagnostics",
        )
        self.assertFalse(data["valid"])
        self.assertTrue(
            any(
                item["type"] == "active_stage_in_inactive_pipeline"
                for item in data["orphan_references"]
            )
        )

    def test_ai_response_policy_test_is_no_side_effect(self):
        OrgInfo.objects.create(
            organization=self.organization,
            ai_playbook="## Rules\nAnswer customer questions using approved business information.",
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            raw="diagnostic-ai-policy",
        )
        data = self._ok(
            bearer,
            "test_ai_response_policy",
        )
        self.assertTrue(data["valid"])
        self.assertFalse(data["side_effects"])
        self.assertEqual(data["messages_sent"], 0)
        self.assertEqual(data["leads_created"], 0)

    def test_workflow_trace_identifies_first_failing_component(self):
        rule = SmartTrigger.objects.create(
            organization=self.organization,
            name="Trace Workflow",
            enabled=True,
            is_active=True,
            position=1,
            trigger_type="lead_created",
            conditions={
                "scopes": [
                    {
                        "pipeline": str(self.pipeline.id),
                        "stages": [str(self.stage.id)],
                    }
                ],
                "sources": [],
                "attributes": [],
            },
            action_type="message",
            action={
                "body": "Hello",
                "account": "00000000-0000-0000-0000-000000000001",
                "schedule": "relative",
                "duration": 0,
                "unit": "minutes",
            },
            fingerprint="trace-workflow-fingerprint",
            created_by=self.admin,
        )
        event = TriggerEvent.objects.create(
            organization=self.organization,
            lead=self.lead,
            kind="lead_created",
            key="trace-workflow-event",
            payload={},
        )
        run = TriggerRun.objects.create(
            rule=rule,
            event=event,
            lead=self.lead,
            action_type="message",
            action=rule.action,
            status="blocked",
            detail="No active WhatsApp reply window.",
            due_at=timezone.now(),
            finished_at=timezone.now(),
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            raw="diagnostic-workflow-trace",
        )
        trace = self._ok(
            bearer,
            "get_workflow_trace",
            {"lead_id": str(self.lead.id), "limit": 20},
        )
        self.assertEqual(trace["first_failure"]["component"], "delivery_policy")
        self.assertEqual(trace["first_failure"]["run_id"], str(run.id))
        self.assertEqual(trace["first_failure"]["status"], "blocked")
