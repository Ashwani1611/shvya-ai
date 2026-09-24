import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.accounts.models import User

from apps.integrations.diagnostic_auth import (
    DIAGNOSTICS_SCOPE,
    validate_authorization_request as validate_diagnostic_authorization,
)
from apps.integrations.mcp_oauth_clients import (
    MCPClientMetadataError,
    fetch_cimd_metadata,
    fetch_operations_cimd_metadata,
    is_allowed_cimd_url,
    is_allowed_external_ai_redirect,
)
from apps.integrations.mcp_schema import (
    MCPInputValidationError,
    validate_mcp_arguments,
)
from apps.integrations.models import (
    DiagnosticOAuthClient,
    OperationsAuditEvent,
    OperationsOAuthClient,
    OperationsPolicy,
)
from apps.integrations.operations_audit import (
    organization_visible_audit_reason,
)
from apps.integrations.operations_auth import (
    OPERATIONS_READ_SCOPE,
    validate_authorization_request as validate_operations_authorization,
)
from apps.integrations.operations_policy import (
    CAP_AUDIT_READ,
    ROLE_ORGANIZATION_ADMIN,
)
from apps.integrations.operations_tools import get_operations_audit
from apps.organizations.models import Organization


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

    @patch("apps.integrations.mcp_oauth_clients.requests.get")
    def test_chatgpt_cimd_plural_public_auth_metadata_is_accepted(self, mocked):
        client_id = "https://chatgpt.com/oauth/client.json"

        class Response:
            status_code = 200
            headers = {}
            content = (
                b'{"client_id":"https://chatgpt.com/oauth/client.json",'
                b'"client_name":"ChatGPT",'
                b'"redirect_uris":["https://chatgpt.com/connector_platform_oauth_redirect"],'
                b'"grant_types":["authorization_code","refresh_token"],'
                b'"response_types":["code"],'
                b'"token_endpoint_auth_methods_supported":["none"],'
                b'"token_endpoint_auth_method":"none"}'
            )

            def json(self):
                return {
                    "client_id": client_id,
                    "client_name": "ChatGPT",
                    "redirect_uris": [
                        "https://chatgpt.com/connector_platform_oauth_redirect"
                    ],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "token_endpoint_auth_method": "none",
                }

        mocked.return_value = Response()
        with patch(
            "apps.integrations.mcp_oauth_clients._resolved_public_addresses",
            return_value={"203.0.113.10"},
        ):
            metadata = fetch_cimd_metadata(client_id)
        self.assertEqual(metadata["client_id"], client_id)
        self.assertEqual(
            metadata["redirect_uris"],
            ["https://chatgpt.com/connector_platform_oauth_redirect"],
        )

    @patch("apps.integrations.mcp_oauth_clients.requests.get")
    def test_claude_cimd_ignores_unimplemented_jwt_bearer_grant(self, mocked):
        client_id = "https://claude.ai/oauth/mcp-oauth-client-metadata"
        payload = {
            "client_id": client_id,
            "client_name": "Claude",
            "redirect_uris": [
                "https://claude.ai/api/mcp/auth_callback",
            ],
            "grant_types": [
                "authorization_code",
                "refresh_token",
                "urn:ietf:params:oauth:grant-type:jwt-bearer",
            ],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

        class Response:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = json.dumps(payload).encode("utf-8")

        mocked.return_value = Response()
        with patch(
            "apps.integrations.mcp_oauth_clients._resolved_public_addresses",
            return_value={"203.0.113.11"},
        ):
            metadata = fetch_operations_cimd_metadata(client_id)

        self.assertEqual(
            metadata["grant_types"],
            ["authorization_code", "refresh_token"],
        )

    @patch("apps.integrations.mcp_oauth_clients.socket.getaddrinfo")
    def test_cimd_rejects_allowlisted_hostname_resolving_to_private_ip(self, resolver):
        resolver.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]
        with self.assertRaises(MCPClientMetadataError):
            fetch_operations_cimd_metadata(
                "https://claude.ai/oauth/mcp-oauth-client-metadata"
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


class OperationsAuditPresentationTests(TestCase):
    def test_org_admin_audit_api_hides_internal_support_reason(self):
        organization = Organization.objects.create(
            name="Audit Presentation Org"
        )
        admin = User.objects.create_user(
            email="audit-present@example.com",
            password="test-password",
            name="Audit Admin",
            organization=organization,
            role=User.Role.ADMIN,
        )
        OperationsPolicy.objects.create(
            organization=organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_AUDIT_READ],
            approval_required_capabilities=[],
        )
        event = OperationsAuditEvent.objects.create(
            actor=admin,
            role="SHVYA_SUPERADMIN",
            organization=organization,
            tool_name="select_organization_context",
            capability="organization.read",
            target_type="organization",
            target_id=str(organization.id),
            reason="Internal incident context that customer must not see",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="e" * 64,
            change_summary={"support_context": "started"},
        )
        identity = SimpleNamespace(
            role=ROLE_ORGANIZATION_ADMIN,
            organization=organization,
            active_organization=None,
            actor=admin,
            granted_capabilities=frozenset({CAP_AUDIT_READ}),
        )

        execution = get_operations_audit(
            identity=identity,
            arguments={"audit_event_id": str(event.id)},
        )
        row = execution.data["events"][0]
        self.assertEqual(
            row["reason"],
            "SHVYA Support context started for this organization.",
        )
        self.assertNotIn("Internal incident", row["reason"])
