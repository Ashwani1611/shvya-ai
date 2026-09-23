"""Setup capabilities remain explicit, consent-bound and tenant-scoped."""

from django.contrib.sessions.backends.db import SessionStore
from django.test import SimpleTestCase
from django.urls import reverse

from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.integrations.mcp_schema import MCPInputValidationError, validate_mcp_arguments
from apps.integrations.operations.setup_catalog import SETUP_TOOL_CAPABILITIES
from apps.integrations.operations.tool_catalog import TOOL_DEFINITIONS, TOOL_INPUT_SCHEMAS
from apps.integrations.operations_auth import OPERATIONS_READ_SCOPE, token_hash
from apps.integrations.operations_models import OperationsOAuthToken, OperationsPolicy
from apps.integrations.operations_policy import (
    ALL_CAPABILITIES,
    CAPABILITY_LABELS,
    CAP_AUDIT_READ,
    CAP_DIAGNOSTICS_READ,
    CAP_ORGANIZATION_READ,
    CAP_SETUP_ARTIFACTS_PREPARE,
    CAP_SETUP_INTAKE_READ,
    CAP_SETUP_INTAKE_WRITE,
    CAP_SETUP_LIBRARY_READ,
    DEFAULT_APPROVAL_REQUIRED,
    DEFAULT_ORG_CAPABILITIES,
    READ_CAPABILITIES,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    WRITE_CAPABILITIES,
    approval_required,
    capabilities_for_grant,
)
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


SETUP_CAPABILITIES = {
    CAP_SETUP_LIBRARY_READ,
    CAP_SETUP_ARTIFACTS_PREPARE,
    CAP_SETUP_INTAKE_READ,
    CAP_SETUP_INTAKE_WRITE,
}


class SetupCapabilityContractTests(SimpleTestCase):
    def test_setup_capabilities_are_explicit_and_do_not_expand_default_grants(self):
        self.assertEqual(DEFAULT_ORG_CAPABILITIES, [
            CAP_ORGANIZATION_READ, CAP_DIAGNOSTICS_READ, CAP_AUDIT_READ,
        ])
        self.assertTrue(SETUP_CAPABILITIES.issubset(ALL_CAPABILITIES))
        self.assertTrue(SETUP_CAPABILITIES.isdisjoint(DEFAULT_ORG_CAPABILITIES))
        self.assertIn(CAP_SETUP_INTAKE_WRITE, DEFAULT_APPROVAL_REQUIRED)
        self.assertIn(CAP_SETUP_INTAKE_WRITE, WRITE_CAPABILITIES)
        for capability in SETUP_CAPABILITIES - {CAP_SETUP_INTAKE_WRITE}:
            self.assertIn(capability, READ_CAPABILITIES)
            self.assertNotIn(capability, WRITE_CAPABILITIES)

    def test_superadmin_read_grant_includes_drafts_but_not_intake_mutations(self):
        granted = capabilities_for_grant(role=ROLE_SUPERADMIN, allow_writes=False)
        self.assertTrue((SETUP_CAPABILITIES - {CAP_SETUP_INTAKE_WRITE}).issubset(granted))
        self.assertNotIn(CAP_SETUP_INTAKE_WRITE, granted)
        self.assertTrue(approval_required(
            role=ROLE_SUPERADMIN, organization=None, capability=CAP_SETUP_INTAKE_WRITE,
        ))

    def test_setup_discovery_advertises_appropriate_scopes_and_mutation_controls(self):
        tools = {item["name"]: item for item in TOOL_DEFINITIONS}
        for name, capability in SETUP_TOOL_CAPABILITIES.items():
            definition = tools[name]
            write = capability == CAP_SETUP_INTAKE_WRITE
            self.assertEqual(definition["annotations"]["readOnlyHint"], not write)
            self.assertEqual(
                "operations.write" in definition["securitySchemes"][0]["scopes"], write,
            )
            self.assertFalse(definition["inputSchema"]["additionalProperties"])
            if write:
                properties = definition["inputSchema"]["properties"]
                for key in ("reason", "dry_run", "approved", "approval_event_id"):
                    self.assertIn(key, properties)

    def test_setup_schemas_reject_unbounded_or_unadvertised_arguments(self):
        for name, arguments in (
            ("list_setup_library", {"limit": 101}),
            ("get_setup_library_resource", {"resource_id": "company-about", "limit": 20001}),
            ("get_setup_variable_schema", {"path": "/etc/passwd"}),
            ("render_setup_template", {"template_id": "shell", "variables": {}}),
            ("analyze_setup_group_export", {"data": {}, "chat_id": "1", "timezone": "UTC", "limit": 501}),
            ("get_setup_intake", {"organization_id": "other-tenant"}),
            ("get_setup_intake", {"limit": 51}),
            ("archive_setup_intake_entry", {"entry_id": "invalid", "expected_revision": 1, "reason": "Archive reviewed intake"}),
        ):
            with self.subTest(tool=name, arguments=arguments):
                with self.assertRaises(MCPInputValidationError):
                    validate_mcp_arguments(arguments, TOOL_INPUT_SCHEMAS[name])


class SetupCapabilityAuthorizationTests(OperationsMCPBase):
    def _policy(self, capabilities):
        return OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=list(capabilities),
            approval_required_capabilities=[CAP_SETUP_INTAKE_WRITE],
        )

    def _admin_token(self, **kwargs):
        return self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            **kwargs,
        )

    def _tool_names(self, bearer):
        return {item["name"] for item in self._list_tools(bearer)["tools"]}

    def test_existing_token_does_not_gain_setup_access_after_policy_expansion(self):
        policy = self._policy(DEFAULT_ORG_CAPABILITIES)
        bearer = self._admin_token()
        policy.allowed_capabilities = [*DEFAULT_ORG_CAPABILITIES, *sorted(SETUP_CAPABILITIES)]
        policy.save(update_fields=["allowed_capabilities", "updated_at"])

        self.assertTrue(set(SETUP_TOOL_CAPABILITIES).isdisjoint(self._tool_names(bearer)))
        context = self._result(self._call(bearer, "get_operations_context"))["structuredContent"]
        self.assertTrue(SETUP_CAPABILITIES.issubset(context["policy_capabilities"]))
        self.assertTrue(SETUP_CAPABILITIES.isdisjoint(context["granted_capabilities"]))
        self.assertTrue(SETUP_CAPABILITIES.isdisjoint(context["capabilities"]))

        denied = self._result(self._call(bearer, "list_setup_library"))
        self.assertTrue(denied["isError"])
        self.assertIn("Fresh SHVYA authorization", denied["structuredContent"]["error"])

        OperationsOAuthToken.objects.filter(access_token_hash=token_hash(bearer)).delete()
        fresh_bearer = self._admin_token()
        self.assertTrue(set(SETUP_TOOL_CAPABILITIES).issubset(self._tool_names(fresh_bearer)))

    def test_live_setup_policy_reduction_immediately_removes_tool_access(self):
        policy = self._policy(SETUP_CAPABILITIES)
        bearer = self._admin_token()
        self.assertIn("list_setup_library", self._tool_names(bearer))
        policy.allowed_capabilities = [CAP_SETUP_INTAKE_READ]
        policy.save(update_fields=["allowed_capabilities", "updated_at"])

        names = self._tool_names(bearer)
        self.assertIn("get_setup_intake", names)
        self.assertNotIn("list_setup_library", names)
        self.assertNotIn("render_setup_template", names)
        self.assertNotIn("upsert_setup_intake_entry", names)
        denied = self._result(self._call(bearer, "list_setup_library"))
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["status"], "SUPERADMIN_REQUIRED")

    def test_library_grant_does_not_grant_artifact_preparation_or_intake(self):
        self._policy([CAP_SETUP_LIBRARY_READ])
        bearer = self._admin_token(scopes=[OPERATIONS_READ_SCOPE])
        names = self._tool_names(bearer)
        self.assertEqual(
            names & set(SETUP_TOOL_CAPABILITIES),
            {"list_setup_library", "get_setup_library_resource", "get_setup_variable_schema"},
        )

    def test_read_scope_cannot_use_intake_write_even_with_stored_capability(self):
        self._policy(SETUP_CAPABILITIES)
        bearer = self._admin_token(
            scopes=[OPERATIONS_READ_SCOPE], granted_capabilities=sorted(SETUP_CAPABILITIES),
        )
        names = self._tool_names(bearer)
        self.assertIn("render_setup_template", names)
        self.assertIn("get_setup_intake", names)
        self.assertNotIn("upsert_setup_intake_entry", names)
        self.assertNotIn("archive_setup_intake_entry", names)
        denied = self._result(self._call(bearer, "archive_setup_intake_entry", {
            "entry_id": str(self.organization.id),
            "expected_revision": 1,
            "reason": "Archive reviewed setup source",
        }))
        self.assertTrue(denied["isError"])
        self.assertIn("operations.write", denied["structuredContent"]["error"])

    def test_setup_allowed_capabilities_appear_unchecked_in_superadmin_policy_ui(self):
        session = SessionStore()
        set_authenticated_user(session, self.superadmin)
        session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = session.session_key
        response = self.client.get(reverse(
            "superadmin-organization-detail", kwargs={"organization_id": self.organization.id},
        ))
        self.assertEqual(response.status_code, 200)
        rows = {item["key"]: item for item in response.context["operations_capabilities"]}
        for capability in SETUP_CAPABILITIES:
            self.assertIn(capability, rows)
            self.assertEqual(rows[capability]["label"], CAPABILITY_LABELS[capability])
            self.assertFalse(rows[capability]["allowed"])
            self.assertEqual(rows[capability]["is_write"], capability == CAP_SETUP_INTAKE_WRITE)
            self.assertContains(response, CAPABILITY_LABELS[capability])

    def test_superadmin_can_save_the_new_allowed_capabilities_and_intake_approval(self):
        session = SessionStore()
        set_authenticated_user(session, self.superadmin)
        session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = session.session_key
        response = self.client.post(reverse(
            "superadmin-organization-operations-mcp-policy", kwargs={"organization_id": self.organization.id},
        ), {
            "organization_admin_enabled": "on",
            "allowed_capabilities": sorted(SETUP_CAPABILITIES),
            "approval_required_capabilities": [CAP_SETUP_INTAKE_WRITE],
        })
        self.assertEqual(response.status_code, 302)
        policy = OperationsPolicy.objects.get(organization=self.organization)
        self.assertEqual(set(policy.allowed_capabilities), SETUP_CAPABILITIES)
        self.assertEqual(policy.approval_required_capabilities, [CAP_SETUP_INTAKE_WRITE])
