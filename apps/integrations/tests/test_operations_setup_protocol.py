"""Native setup prompts/resources share Operations authority and safe auditing."""

import json
from unittest.mock import patch

from apps.integrations.operations import setup_library
from apps.integrations.operations_auth import OPERATIONS_READ_SCOPE
from apps.integrations.operations_models import OperationsAuditEvent, OperationsPolicy
from apps.integrations.operations_policy import (
    CAP_ORGANIZATION_READ,
    CAP_SETUP_ARTIFACTS_PREPARE,
    CAP_SETUP_LIBRARY_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
)
from apps.integrations.operations_tools import ToolExecution
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


class SetupNativeProtocolTests(OperationsMCPBase):
    def _rpc(self, method, params=None, *, bearer=None, modern=False, **headers):
        if bearer:
            headers["HTTP_AUTHORIZATION"] = "Bearer " + bearer
        if modern:
            headers["HTTP_MCP_PROTOCOL_VERSION"] = "2026-07-28"
            headers["HTTP_MCP_METHOD"] = method
        return self.client.post(
            "/operations/mcp/",
            data=json.dumps({
                "jsonrpc": "2.0", "id": "native-setup", "method": method,
                "params": {} if params is None else params,
            }),
            content_type="application/json", **headers,
        )

    def _superadmin_token(self, **kwargs):
        return self._token(actor=self.superadmin, role=ROLE_SUPERADMIN, **kwargs)

    def _admin_token(self, capabilities):
        policy = OperationsPolicy.objects.create(
            organization=self.organization, organization_admin_enabled=True,
            allowed_capabilities=capabilities, approval_required_capabilities=[],
        )
        return policy, self._token(
            actor=self.admin, role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization, scopes=[OPERATIONS_READ_SCOPE],
        )

    def test_setup_protocol_capabilities_are_advertised_in_both_discovery_versions(self):
        for method, modern in (("initialize", False), ("server/discover", True)):
            result = self._rpc(method, modern=modern).json()["result"]
            self.assertEqual(set(result["capabilities"]), {"tools", "prompts", "resources"})

    def test_native_reads_require_bearer_authentication(self):
        for method, params in (
            ("prompts/list", {}), ("resources/list", {}),
            ("prompts/get", {"name": "shvya-operator"}),
            ("resources/read", {"uri": "shvya-kit:///README.md"}),
        ):
            response = self._rpc(method, params)
            self.assertEqual(response.status_code, 401)
            self.assertIn("WWW-Authenticate", response)
            self.assertEqual(response.json()["error"]["code"], -32001)
            self.assertNotIn("result", response.json())

    def test_superadmin_can_read_static_catalog_without_an_organization(self):
        bearer = self._superadmin_token(scopes=[OPERATIONS_READ_SCOPE])
        prompts = self._rpc("prompts/list", bearer=bearer, modern=True)
        self.assertEqual(prompts.status_code, 200)
        result = prompts.json()["result"]
        self.assertEqual(result["prompts"], setup_library.list_prompts()["prompts"])
        self.assertNotIn("structuredContent", result)
        self.assertEqual(result["resultType"], "complete")
        resources = self._rpc("resources/list", bearer=bearer).json()["result"]
        self.assertEqual(resources["resources"], setup_library.list_resources()["resources"])
        event = OperationsAuditEvent.objects.get(tool_name="prompts/list")
        self.assertIsNone(event.organization)
        self.assertIsNone(event.support_session)
        self.assertEqual(event.capability, CAP_SETUP_LIBRARY_READ)
        self.assertEqual(event.target_type, "platform")

    def test_resources_are_read_in_exact_chunks_with_explicit_continuation(self):
        bearer = self._superadmin_token()
        uri = setup_library.list_resources()["resources"][0]["uri"]
        first = self._rpc("resources/read", {"uri": uri, "limit": 100}, bearer=bearer)
        self.assertEqual(first.status_code, 200)
        result = first.json()["result"]
        expected = setup_library.read_resource(uri, limit=100)
        self.assertEqual(result["contents"], expected["contents"])
        self.assertTrue(result["_meta"]["truncated"])
        self.assertEqual(result["_meta"]["next_offset"], 100)
        second = self._rpc("resources/read", {"uri": uri, "offset": 100, "limit": 100}, bearer=bearer)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["result"]["contents"], setup_library.read_resource(uri, offset=100, limit=100)["contents"])

    def test_prompt_context_is_data_and_does_not_enter_audit(self):
        policy, bearer = self._admin_token([CAP_SETUP_LIBRARY_READ])
        unique_context = "Please compare Acme onboarding against the supplied September requirements."
        response = self._rpc("prompts/get", {
            "name": "shvya-operator",
            "arguments": {"organization_name": "Another company", "task": unique_context},
        }, bearer=bearer)
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        text = result["messages"][0]["content"]["text"]
        self.assertEqual(result["messages"][0]["role"], "user")
        self.assertIn(unique_context, text)
        self.assertIn("not tenant selection or authorization", text)
        event = OperationsAuditEvent.objects.get(tool_name="prompts/get")
        self.assertEqual(event.organization, policy.organization)
        self.assertEqual(event.change_summary["context_field_count"], 2)
        audit = json.dumps({"summary": event.change_summary, "reason": event.reason})
        self.assertNotIn(unique_context, audit)
        self.assertNotIn("Another company", audit)

    def test_native_prompt_arguments_reject_secrets_and_keep_audits_safe(self):
        bearer = self._superadmin_token()
        credential = "Bearer abcdefghijklmnopqrstuvwxyz1234567890"
        response = self._rpc("prompts/get", {
            "name": "shvya-operator", "arguments": {"task": credential},
        }, bearer=bearer)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(credential, response.content.decode())
        event = OperationsAuditEvent.objects.get(tool_name="prompts/get")
        self.assertNotIn(credential, json.dumps(event.change_summary))
        self.assertNotIn(credential, event.reason)

    def test_native_resources_reject_unknown_uris_and_paths(self):
        bearer = self._superadmin_token()
        for uri in (
            "file:///etc/passwd", "shvya-kit:///../../../../etc/passwd",
            "https://example.com/prompt", "shvya-kit:///README.md?secret=1",
        ):
            response = self._rpc("resources/read", {"uri": uri}, bearer=bearer)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], -32002)
            self.assertNotIn(uri, response.content.decode())

    def test_native_params_are_strict_and_bounded(self):
        bearer = self._superadmin_token()
        for method, params in (
            ("resources/list", []),
            ("prompts/list", {"cursor": "unrecognized"}),
            ("prompts/list", {"reason": "Raw user content must not become an audit reason"}),
            ("prompts/list", {"_meta": {"extra": "x" * 4097}}),
            ("prompts/get", {"name": "shvya-operator", "arguments": {"task": "x" * 4001}}),
            ("prompts/get", {"name": "shvya-operator", "arguments": {"organization_id": str(self.other_organization.id)}}),
            ("resources/read", {"uri": "shvya-kit:///README.md", "limit": 20001}),
        ):
            response = self._rpc(method, params, bearer=bearer)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.json()["error"]["code"], -32602)
        self.assertFalse(OperationsAuditEvent.objects.filter(reason__contains="Raw user content").exists())

    def test_native_access_honors_frozen_grants_and_live_policy_reduction(self):
        policy, bearer = self._admin_token([CAP_ORGANIZATION_READ])
        policy.allowed_capabilities = [CAP_ORGANIZATION_READ, CAP_SETUP_LIBRARY_READ]
        policy.save(update_fields=["allowed_capabilities", "updated_at"])
        response = self._rpc("prompts/list", bearer=bearer)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Fresh SHVYA authorization", response.json()["error"]["message"])

        policy.allowed_capabilities = [CAP_ORGANIZATION_READ]
        policy.save(update_fields=["allowed_capabilities", "updated_at"])
        response = self._rpc("resources/list", bearer=bearer)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["data"]["code"], "operations_superadmin_required")

    def test_old_superadmin_grant_cannot_read_new_setup_protocol_surfaces(self):
        bearer = self._superadmin_token(granted_capabilities=[CAP_ORGANIZATION_READ])
        response = self._rpc("prompts/list", bearer=bearer)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Fresh SHVYA authorization", response.json()["error"]["message"])

    def test_native_modern_prompt_name_header_must_match(self):
        bearer = self._superadmin_token()
        response = self._rpc("prompts/get", {"name": "shvya-operator"}, bearer=bearer, modern=True,
                             HTTP_MCP_NAME="another-prompt")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Mcp-Name", response.json()["error"]["message"])

    def test_variable_schema_tool_retains_literal_static_variables(self):
        bearer = self._superadmin_token()
        response = self._result(self._call(bearer, "get_setup_variable_schema"))
        self.assertFalse(response["isError"])
        data = response["structuredContent"]
        # The wrapper may add metadata, but the literal variable contract is
        # retained even when its keys contain words such as native_runtime_variables.
        serialized = json.dumps(data)
        for variable in setup_library.variable_schema()["variables"]:
            self.assertIn(variable["name"], serialized)
        self.assertIn("SHVYA_", serialized)

    def test_setup_tool_responses_report_when_generic_redaction_changed_them(self):
        bearer = self._superadmin_token()
        with patch("apps.integrations.views.operations_mcp.execute_operations_tool", return_value=ToolExecution(
            data={"text": "Bearer abcdefghijklmnopqrstuvwxyz1234567890"},
            capability=CAP_SETUP_ARTIFACTS_PREPARE, target_type="platform",
        )):
            result = self._result(self._call(bearer, "render_setup_template", {
                "template_id": "company-about", "variables": {},
            }))
        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["response_sanitized"])
        self.assertIn("[REDACTED]", result["structuredContent"]["text"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
