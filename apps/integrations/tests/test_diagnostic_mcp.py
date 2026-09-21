import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.integrations.diagnostic_auth import (
    DIAGNOSTICS_SCOPE,
    OFFLINE_SCOPE,
    pkce_s256,
    sanitize_data,
    sanitize_text,
)
from apps.integrations.models import (
    DiagnosticAccessLog,
    DiagnosticOAuthAuthorizationCode,
    DiagnosticOAuthToken,
)
from apps.organizations.models import APIKey, Organization


class DiagnosticMCPTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Diagnostic Org A")
        self.other_organization = Organization.objects.create(name="Diagnostic Org B")
        self.api_key, self.raw_key = APIKey.issue(
            organization=self.organization,
            name="ChatGPT Diagnostics",
        )
        self.api_key.can_upsert_leads = False
        self.api_key.can_read_diagnostics = True
        self.api_key.save(
            update_fields=["can_upsert_leads", "can_read_diagnostics"]
        )

    def _call(self, name, arguments=None, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = "Bearer " + token
        return self.client.post(
            "/mcp/",
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
            **headers,
        )

    def test_tools_are_discoverable_and_read_only(self):
        response = self.client.post(
            "/mcp/",
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
        tools = response.json()["result"]["tools"]
        self.assertGreaterEqual(len(tools), 10)
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertEqual(tool["securitySchemes"][0]["type"], "oauth2")
            self.assertIn(
                DIAGNOSTICS_SCOPE,
                tool["securitySchemes"][0]["scopes"],
            )

    def test_diagnostic_key_is_read_only_and_workspace_scoped(self):
        self.assertFalse(self.api_key.can_upsert_leads)
        self.assertTrue(self.api_key.can_read_diagnostics)

        response = self._call(
            "get_workspace_profile",
            token=self.raw_key,
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"]["id"],
            str(self.organization.id),
        )

    def test_normal_api_key_is_rejected_by_diagnostics(self):
        normal_key, raw_normal = APIKey.issue(
            organization=self.organization,
            name="Lead API only",
        )
        self.assertTrue(normal_key.can_upsert_leads)
        self.assertFalse(normal_key.can_read_diagnostics)

        response = self._call(
            "get_workspace_profile",
            token=raw_normal,
        )
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])

    def test_missing_auth_returns_oauth_challenge(self):
        response = self._call("get_workspace_profile")
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        challenge = result["_meta"]["mcp/www_authenticate"][0]
        self.assertIn("oauth-protected-resource", challenge)

    def test_audit_log_stores_only_argument_fingerprint(self):
        query = "+919999999999"
        response = self._call(
            "find_leads",
            {"query": query},
            token=self.raw_key,
        )
        self.assertEqual(response.status_code, 200)

        log = DiagnosticAccessLog.objects.get(
            organization=self.organization,
            tool_name="find_leads",
        )
        self.assertEqual(len(log.request_fingerprint), 64)
        self.assertNotIn(query, log.request_fingerprint)

    def test_sanitizer_redacts_secret_named_fields(self):
        external_id = "wamid." + ("A" * 80)
        payload = sanitize_data(
            {
                "api_key": "fixture-value",
                "password": "fixture-value",
                "external_id": external_id,
                "safe": "ok",
            }
        )
        self.assertEqual(payload["api_key"], "[REDACTED]")
        self.assertEqual(payload["password"], "[REDACTED]")
        self.assertEqual(payload["external_id"], external_id)
        self.assertEqual(payload["safe"], "ok")

    def test_tool_schema_rejects_non_object_arguments(self):
        response = self._call(
            "get_workspace_profile",
            "not-an-object",
            token=self.raw_key,
        )
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("must be an object", result["structuredContent"]["error"])

    def test_tool_schema_rejects_unadvertised_arguments(self):
        response = self._call(
            "get_workspace_profile",
            {"unexpected": "value"},
            token=self.raw_key,
        )
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("unexpected field", result["structuredContent"]["error"])

    def test_diagnostic_access_log_is_immutable(self):
        response = self._call(
            "get_workspace_profile",
            token=self.raw_key,
        )
        self.assertEqual(response.status_code, 200)
        log = DiagnosticAccessLog.objects.get(
            organization=self.organization,
            tool_name="get_workspace_profile",
        )
        log.error_code = "rewrite"
        with self.assertRaises(ValidationError):
            log.save(update_fields=["error_code"])
        with self.assertRaises(ValidationError):
            DiagnosticAccessLog.objects.filter(pk=log.pk).update(
                error_code="bulk-rewrite"
            )
        with self.assertRaises(ValidationError):
            DiagnosticAccessLog.objects.filter(pk=log.pk).delete()

    def test_sanitizer_redacts_inline_secret_assignments(self):
        safe = sanitize_text(
            "password=example-value api_key:example-key token=short-token "
            "secret:short-secret credential=short-credential "
            "session=session-value postgres://user:pass@example.test/db"
        )
        for leaked in (
            "example-value",
            "example-key",
            "short-token",
            "short-secret",
            "short-credential",
            "session-value",
            "user:pass",
        ):
            self.assertNotIn(leaked, safe)
        self.assertIn("[REDACTED]", safe)

    def test_modern_server_discovery(self):
        response = self.client.post(
            "/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "discover-1",
                    "method": "server/discover",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {
                                "name": "test-client",
                                "version": "1.0",
                            },
                        }
                    },
                }
            ),
            content_type="application/json",
            HTTP_MCP_PROTOCOL_VERSION="2026-07-28",
            HTTP_MCP_METHOD="server/discover",
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["supportedVersions"], ["2026-07-28"])
        self.assertEqual(result["resultType"], "complete")


class DiagnosticOAuthTests(TestCase):
    callback = "https://chatgpt.com/aip/callback"

    def setUp(self):
        self.organization = Organization.objects.create(name="OAuth Diagnostic Org")
        self.api_key, self.raw_key = APIKey.issue(
            organization=self.organization,
            name="ChatGPT Diagnostics",
        )
        self.api_key.can_upsert_leads = False
        self.api_key.can_read_diagnostics = True
        self.api_key.save(
            update_fields=["can_upsert_leads", "can_read_diagnostics"]
        )

    def _register(self):
        response = self.client.post(
            "/oauth/register",
            data=json.dumps(
                {
                    "client_name": "ChatGPT",
                    "application_type": "web",
                    "redirect_uris": [self.callback],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["client_id"]

    def test_metadata_and_dynamic_registration(self):
        resource = self.client.get("/.well-known/oauth-protected-resource")
        self.assertEqual(resource.status_code, 200)
        self.assertEqual(resource.json()["resource"], "http://testserver/mcp/")

        server = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(server.status_code, 200)
        self.assertEqual(server.json()["code_challenge_methods_supported"], ["S256"])
        self.assertIn("refresh_token", server.json()["grant_types_supported"])
        self.assertEqual(server.json()["token_endpoint_auth_methods_supported"], ["none"])
        self.assertTrue(server.json()["client_id_metadata_document_supported"])
        self.assertTrue(server.json()["revocation_endpoint"].endswith("/oauth/revoke"))

        client_id = self._register()
        self.assertTrue(client_id.startswith("shvya_mcp_"))

    def test_registration_rejects_non_openai_redirect(self):
        response = self.client.post(
            "/oauth/register",
            data=json.dumps(
                {
                    "redirect_uris": ["https://example.com/callback"],
                    "token_endpoint_auth_method": "none",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_pkce_code_is_one_time_and_refresh_rotates_tokens(self):
        client_id = self._register()
        verifier = "v" * 64
        resource = "http://testserver/mcp/"
        scope = f"{DIAGNOSTICS_SCOPE} {OFFLINE_SCOPE}"

        response = self.client.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": self.callback,
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": scope,
                "state": "state-123",
                "resource": resource,
                "diagnostic_api_key": self.raw_key,
            },
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual(query["state"], ["state-123"])
        code = query["code"][0]

        token_response = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": self.callback,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        self.assertEqual(token_response.status_code, 200)
        body = token_response.json()
        self.assertEqual(DiagnosticOAuthToken.objects.count(), 1)
        original_refresh_expiry = DiagnosticOAuthToken.objects.get().refresh_expires_at
        self.assertIsNotNone(
            DiagnosticOAuthAuthorizationCode.objects.get().used_at
        )

        replay = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": self.callback,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        self.assertEqual(replay.status_code, 400)

        refresh = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": body["refresh_token"],
                "resource": resource,
            },
        )
        self.assertEqual(refresh.status_code, 200)
        self.assertNotEqual(refresh.json()["access_token"], body["access_token"])
        self.assertNotEqual(refresh.json()["refresh_token"], body["refresh_token"])
        self.assertEqual(
            DiagnosticOAuthToken.objects.get().refresh_expires_at,
            original_refresh_expiry,
        )
        refreshed_body = refresh.json()
        grant = DiagnosticOAuthToken.objects.get()
        grant.refresh_expires_at = timezone.now() - timedelta(seconds=1)
        grant.expires_at = timezone.now() + timedelta(hours=1)
        grant.save(update_fields=["refresh_expires_at", "expires_at"])
        denied = self.client.post(
            "/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "name": "get_workspace_profile",
                        "arguments": {},
                    },
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + refreshed_body["access_token"],
        )
        self.assertTrue(denied.json()["result"]["isError"])

    def test_strict_pkce_rejects_short_challenge(self):
        client_id = self._register()
        response = self.client.get(
            "/oauth/authorize",
            {
                "client_id": client_id,
                "redirect_uri": self.callback,
                "response_type": "code",
                "code_challenge": "short",
                "code_challenge_method": "S256",
                "scope": DIAGNOSTICS_SCOPE,
                "resource": "http://testserver/mcp/",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_revocation_invalidates_diagnostic_grant(self):
        client_id = self._register()
        verifier = "r" * 64
        resource = "http://testserver/mcp/"
        authorize = self.client.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": self.callback,
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": f"{DIAGNOSTICS_SCOPE} {OFFLINE_SCOPE}",
                "resource": resource,
                "diagnostic_api_key": self.raw_key,
            },
        )
        code = parse_qs(urlparse(authorize["Location"]).query)["code"][0]
        token_response = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": self.callback,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        access = token_response.json()["access_token"]
        revoke = self.client.post("/oauth/revoke", data={"token": access})
        self.assertEqual(revoke.status_code, 200)
        self.assertIsNotNone(DiagnosticOAuthToken.objects.get().revoked_at)
        denied = self._call("get_workspace_profile", token=access)
        self.assertTrue(denied.json()["result"]["isError"])
        self.assertTrue(
            DiagnosticAccessLog.objects.filter(tool_name="oauth_revoke").exists()
        )

    def test_expired_api_key_invalidates_existing_oauth_grant(self):
        client_id = self._register()
        verifier = "e" * 64
        resource = "http://testserver/mcp/"
        authorize = self.client.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": self.callback,
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": f"{DIAGNOSTICS_SCOPE} {OFFLINE_SCOPE}",
                "resource": resource,
                "diagnostic_api_key": self.raw_key,
            },
        )
        code = parse_qs(urlparse(authorize["Location"]).query)["code"][0]
        token_response = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": self.callback,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        body = token_response.json()
        self.api_key.expires_at = timezone.now() - timedelta(seconds=1)
        self.api_key.save(update_fields=["expires_at"])

        denied = self._call(
            "get_workspace_profile",
            token=body["access_token"],
        )
        self.assertTrue(denied.json()["result"]["isError"])
        refresh = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": body["refresh_token"],
                "resource": resource,
            },
        )
        self.assertEqual(refresh.status_code, 400)

    def test_wrong_resource_does_not_mint_token(self):
        client_id = self._register()
        verifier = "x" * 64
        resource = "http://testserver/mcp/"

        response = self.client.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": self.callback,
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": DIAGNOSTICS_SCOPE,
                "resource": resource,
                "diagnostic_api_key": self.raw_key,
            },
        )
        code = parse_qs(urlparse(response["Location"]).query)["code"][0]

        token_response = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": self.callback,
                "code_verifier": verifier,
                "resource": "http://testserver/wrong-resource/",
            },
        )
        self.assertEqual(token_response.status_code, 400)
        self.assertEqual(DiagnosticOAuthToken.objects.count(), 0)
