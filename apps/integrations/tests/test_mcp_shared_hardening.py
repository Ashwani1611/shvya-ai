from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.integrations.diagnostic_auth import (
    DIAGNOSTICS_SCOPE,
    validate_authorization_request as validate_diagnostic_authorization,
)
from apps.integrations.mcp_oauth_clients import (
    is_allowed_cimd_url,
    is_allowed_external_ai_redirect,
)
from apps.integrations.mcp_schema import (
    MCPInputValidationError,
    validate_mcp_arguments,
)
from apps.integrations.models import (
    DiagnosticOAuthClient,
    OperationsOAuthClient,
)
from apps.integrations.operations_audit import (
    organization_visible_audit_reason,
)
from apps.integrations.operations_auth import (
    OPERATIONS_READ_SCOPE,
    validate_authorization_request as validate_operations_authorization,
)


class MCPSharedValidationTests(SimpleTestCase):
    def test_schema_validator_rejects_extra_nested_fields(self):
        schema = {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                }
            },
            "required": ["changes"],
            "additionalProperties": False,
        }
        with self.assertRaises(MCPInputValidationError):
            validate_mcp_arguments(
                {"changes": {"enabled": True, "secret": "x"}},
                schema,
            )

    def test_schema_validator_enforces_uuid_enum_and_range(self):
        schema = {
            "type": "object",
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "mode": {"type": "string", "enum": ["safe"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["id", "mode", "limit"],
            "additionalProperties": False,
        }
        with self.assertRaises(MCPInputValidationError):
            validate_mcp_arguments(
                {"id": "not-a-uuid", "mode": "unsafe", "limit": 10},
                schema,
            )

    def test_cimd_and_redirect_allowlists_reject_untrusted_hosts(self):
        self.assertTrue(
            is_allowed_cimd_url(
                "https://chatgpt.com/.well-known/shvya-client.json"
            )
        )
        self.assertFalse(
            is_allowed_cimd_url(
                "https://example.com/.well-known/shvya-client.json"
            )
        )
        self.assertFalse(
            is_allowed_cimd_url(
                "https://chatgpt.com/../client.json"
            )
        )
        self.assertTrue(
            is_allowed_external_ai_redirect(
                "http://127.0.0.1:33418/"
            )
        )
        self.assertFalse(
            is_allowed_external_ai_redirect(
                "http://127.0.0.1:9999/"
            )
        )
        self.assertFalse(
            is_allowed_external_ai_redirect(
                "https://vscode.dev/arbitrary"
            )
        )

    def test_org_visible_support_reason_hides_internal_context_reason(self):
        event = SimpleNamespace(
            tool_name="select_organization_context",
            reason="Internal security investigation: do not expose",
        )
        visible = organization_visible_audit_reason(event)
        self.assertEqual(
            visible,
            "SHVYA Support context started for this organization.",
        )
        self.assertNotIn("security investigation", visible)


class MCPCIMDAuthorizationTests(TestCase):
    client_id = "https://chatgpt.com/.well-known/shvya-client.json"
    callback = "https://chatgpt.com/aip/callback"
    challenge = "a" * 43

    @property
    def metadata(self):
        return {
            "client_id": self.client_id,
            "client_name": "ChatGPT",
            "application_type": "web",
            "redirect_uris": [self.callback],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    @patch("apps.integrations.diagnostic_auth.fetch_cimd_metadata")
    def test_diagnostic_authorization_accepts_cimd(self, mocked):
        mocked.return_value = self.metadata
        client = validate_diagnostic_authorization(
            client_id=self.client_id,
            redirect_uri=self.callback,
            response_type="code",
            code_challenge=self.challenge,
            code_challenge_method="S256",
            scope=DIAGNOSTICS_SCOPE,
        )
        self.assertEqual(client.client_id, self.client_id)
        self.assertTrue(
            DiagnosticOAuthClient.objects.filter(
                client_id=self.client_id
            ).exists()
        )

    @patch("apps.integrations.operations_auth.fetch_cimd_metadata")
    def test_operations_authorization_accepts_cimd(self, mocked):
        mocked.return_value = self.metadata
        client = validate_operations_authorization(
            client_id=self.client_id,
            redirect_uri=self.callback,
            response_type="code",
            code_challenge=self.challenge,
            code_challenge_method="S256",
            scope=OPERATIONS_READ_SCOPE,
        )
        self.assertEqual(client.client_id, self.client_id)
        self.assertTrue(
            OperationsOAuthClient.objects.filter(
                client_id=self.client_id
            ).exists()
        )
