# ruff: noqa: F403,F405
"""Domain-focused coverage split from the historical Operations MCP suite."""

from apps.integrations.tests.operations_mcp_test_base import *


class TestOperationsMCPOAuth(OperationsMCPBase):
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

    def test_read_only_oauth_consent_hides_write_capabilities(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[
                CAP_LEAD_STAGE_WRITE,
                CAP_AI_CONFIG_WRITE,
            ],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )
        verifier = "d" * 64

        response = self.client.get(
            "/operations/oauth/authorize",
            {
                "client_id": self.oauth_client.client_id,
                "redirect_uri": "https://chatgpt.com/aip/callback",
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": OPERATIONS_READ_SCOPE,
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Read SHVYA Operations", body)
        self.assertNotIn("Request write capability", body)
        self.assertIn(
            "Read business &amp; CRM configuration",
            body,
        )
        self.assertNotIn(
            "Move leads between active stages/pipelines",
            body,
        )
        self.assertNotIn(
            "Update organization AI profile / Playbook",
            body,
        )

    def test_oauth_consent_expands_legacy_approval_policy(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_CRM_CONFIG_WRITE],
            approval_required_capabilities=[CAP_CRM_CONFIG_WRITE],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )

        verifier = "l" * 64
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
        options = response.context["identity_options"]
        organization_option = next(
            item
            for item in options
            if item["role"] == ROLE_ORGANIZATION_ADMIN
        )
        capabilities = {
            item["key"]: item
            for item in organization_option["capabilities"]
        }
        for capability in (
            CAP_PIPELINE_CONFIG_WRITE,
            CAP_STAGE_CONFIG_WRITE,
            CAP_ATTRIBUTE_CONFIG_WRITE,
        ):
            self.assertIn(capability, capabilities)
            self.assertTrue(
                capabilities[capability]["approval_required"]
            )

    def test_standard_operations_resource_metadata_discovery(self):
        for path in (
            "/.well-known/oauth-protected-resource/operations/mcp/",
            "/.well-known/oauth-protected-resource/operations/mcp",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(
                body["resource"],
                "http://testserver/operations/mcp/",
            )
            self.assertEqual(
                body["authorization_servers"],
                ["http://testserver/operations"],
            )
            self.assertEqual(
                body["bearer_methods_supported"],
                ["header"],
            )
            self.assertIn(
                OPERATIONS_READ_SCOPE,
                body["scopes_supported"],
            )
            self.assertIn(
                OPERATIONS_WRITE_SCOPE,
                body["scopes_supported"],
            )

        server = self.client.get(
            "/.well-known/oauth-authorization-server/operations"
        )
        self.assertEqual(server.status_code, 200)
        self.assertEqual(
            server.json()["code_challenge_methods_supported"],
            ["S256"],
        )

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

    def test_builtin_claude_browser_public_client_uses_exact_callback(self):
        verifier = "c" * 64
        response = self.client.get(
            "/operations/oauth/authorize",
            {
                "client_id": CLAUDE_BROWSER_CLIENT_ID,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": (
                    f"{OPERATIONS_READ_SCOPE} "
                    f"{OPERATIONS_WRITE_SCOPE} "
                    f"{OFFLINE_SCOPE}"
                ),
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(response.status_code, 200)
        client = OperationsOAuthClient.objects.get(
            client_id=CLAUDE_BROWSER_CLIENT_ID
        )
        self.assertEqual(
            client.redirect_uris,
            [
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
            ],
        )
        self.assertEqual(client.application_type, "web")
        self.assertEqual(
            client.grant_types,
            ["authorization_code", "refresh_token"],
        )
        self.assertEqual(client.response_types, ["code"])

        rejected = self.client.get(
            "/operations/oauth/authorize",
            {
                "client_id": CLAUDE_BROWSER_CLIENT_ID,
                "redirect_uri": "https://example.com/callback",
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": f"{OPERATIONS_READ_SCOPE} {OFFLINE_SCOPE}",
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(
            rejected,
            "OAuth redirect URI is not registered.",
            status_code=400,
        )

    def test_registration_accepts_remote_web_and_native_loopback_callbacks(self):
        for callback in (
            "https://chatgpt.com/aip/callback",
            "https://vscode.dev/redirect",
            "https://www.cursor.com/agents/mcp/oauth/callback",
            "https://custom-mcp-client.example/callback",
            "http://127.0.0.1/callback",
            "http://localhost:54321/oauth/callback",
            "http://[::1]:49152/oauth/callback",
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
            self.assertEqual(
                response.status_code,
                201,
                callback,
            )

        for unsafe_callback in (
            "http://example.com/callback",
            "http://localhost:7777/callback#fragment",
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
            self.assertEqual(
                rejected.status_code,
                400,
                unsafe_callback,
            )

    def test_portless_loopback_registration_accepts_ephemeral_runtime_port(self):
        registration = self.client.post(
            "/operations/oauth/register",
            data=json.dumps(
                {
                    "client_name": "Native MCP",
                    "application_type": "native",
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(registration.status_code, 201)
        client_id = registration.json()["client_id"]
        common = {
            "client_id": client_id,
            "response_type": "code",
            "code_challenge": "A" * 43,
            "code_challenge_method": "S256",
            "scope": f"{OPERATIONS_READ_SCOPE} {OFFLINE_SCOPE}",
            "resource": "http://testserver/operations/mcp/",
        }

        accepted = self.client.get(
            "/operations/oauth/authorize",
            {
                **common,
                "redirect_uri": "http://127.0.0.1:49152/callback",
            },
        )
        self.assertEqual(accepted.status_code, 200)

        wrong_path = self.client.get(
            "/operations/oauth/authorize",
            {
                **common,
                "redirect_uri": "http://127.0.0.1:49152/other",
            },
        )
        self.assertEqual(wrong_path.status_code, 400)

        wrong_host = self.client.get(
            "/operations/oauth/authorize",
            {
                **common,
                "redirect_uri": "http://localhost:49152/callback",
            },
        )
        self.assertEqual(wrong_host.status_code, 400)

    def test_operations_oauth_bounds_dynamic_registration_and_state(self):
        too_many = self.client.post(
            "/operations/oauth/register",
            data=json.dumps(
                {
                    "redirect_uris": [
                        f"https://chatgpt.com/aip/callback/{index}"
                        for index in range(9)
                    ],
                    "token_endpoint_auth_method": "none",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(too_many.status_code, 400)

        oversized = self.client.post(
            "/operations/oauth/register",
            data=json.dumps(
                {
                    "client_name": "x" * 33000,
                    "redirect_uris": [
                        "https://chatgpt.com/aip/callback"
                    ],
                    "token_endpoint_auth_method": "none",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized["Cache-Control"], "no-store")
        self.assertEqual(oversized["Pragma"], "no-cache")

        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )

        verifier = "z" * 64
        oversized_state = "state-" + ("s" * 2000)
        rejected_state = self.client.post(
            "/operations/oauth/authorize",
            data={
                "client_id": self.oauth_client.client_id,
                "redirect_uri": "https://chatgpt.com/aip/callback",
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": OPERATIONS_READ_SCOPE,
                "resource": "http://testserver/operations/mcp/",
                "state": oversized_state,
                "actor_mode": ROLE_ORGANIZATION_ADMIN,
            },
        )
        self.assertEqual(rejected_state.status_code, 400)
        self.assertContains(
            rejected_state,
            "OAuth state is too long.",
            status_code=400,
        )

        state = "normal-state-value"
        authorize = self.client.post(
            "/operations/oauth/authorize",
            data={
                "client_id": self.oauth_client.client_id,
                "redirect_uri": "https://chatgpt.com/aip/callback",
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": OPERATIONS_READ_SCOPE,
                "resource": "http://testserver/operations/mcp/",
                "state": state,
                "actor_mode": ROLE_ORGANIZATION_ADMIN,
            },
        )
        self.assertEqual(authorize.status_code, 302)
        returned = parse_qs(
            urlparse(authorize["Location"]).query
        )["state"][0]
        self.assertEqual(returned, state)

        huge_token_request = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "password",
                "client_id": self.oauth_client.client_id,
                "padding": "y" * 33000,
            },
        )
        self.assertEqual(huge_token_request.status_code, 413)
        self.assertEqual(
            huge_token_request["Cache-Control"],
            "no-store",
        )

    def test_oauth_authorization_code_rolls_back_if_security_audit_fails(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )
        verifier = "q" * 64

        with patch(
            "apps.integrations.views.operations_mcp._record_oauth_security_event",
            side_effect=RuntimeError("oauth audit unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/operations/oauth/authorize",
                    data={
                        "client_id": self.oauth_client.client_id,
                        "redirect_uri": "https://chatgpt.com/aip/callback",
                        "response_type": "code",
                        "code_challenge": pkce_s256(verifier),
                        "code_challenge_method": "S256",
                        "scope": OPERATIONS_READ_SCOPE,
                        "resource": "http://testserver/operations/mcp/",
                        "actor_mode": ROLE_ORGANIZATION_ADMIN,
                    },
                )

        self.assertFalse(
            OperationsOAuthAuthorizationCode.objects.filter(
                actor=self.admin,
                client=self.oauth_client,
            ).exists()
        )

    def test_oauth_token_issue_rolls_back_if_security_audit_fails(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )
        verifier = "r" * 64
        authorize = self.client.post(
            "/operations/oauth/authorize",
            data={
                "client_id": self.oauth_client.client_id,
                "redirect_uri": "https://chatgpt.com/aip/callback",
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": OPERATIONS_READ_SCOPE,
                "resource": "http://testserver/operations/mcp/",
                "actor_mode": ROLE_ORGANIZATION_ADMIN,
            },
        )
        self.assertEqual(authorize.status_code, 302)
        code = parse_qs(
            urlparse(authorize["Location"]).query
        )["code"][0]
        code_row = OperationsOAuthAuthorizationCode.objects.get(
            actor=self.admin,
            client=self.oauth_client,
        )

        with patch(
            "apps.integrations.views.operations_mcp._record_oauth_security_event",
            side_effect=RuntimeError("oauth token audit unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/operations/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self.oauth_client.client_id,
                        "code": code,
                        "redirect_uri": "https://chatgpt.com/aip/callback",
                        "code_verifier": verifier,
                        "resource": "http://testserver/operations/mcp/",
                    },
                )

        self.assertFalse(
            OperationsOAuthToken.objects.filter(
                actor=self.admin,
                client=self.oauth_client,
            ).exists()
        )
        code_row.refresh_from_db()
        self.assertIsNone(code_row.used_at)

        success = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.oauth_client.client_id,
                "code": code,
                "redirect_uri": "https://chatgpt.com/aip/callback",
                "code_verifier": verifier,
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(success.status_code, 200)

    def test_oauth_refresh_rotation_rolls_back_if_security_audit_fails(self):
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
        token = OperationsOAuthToken.objects.get(actor=self.admin)
        before_access_hash = token.access_token_hash
        before_refresh_hash = token.refresh_token_hash
        before_access_expiry = token.expires_at
        before_refresh_expiry = token.refresh_expires_at

        with patch(
            "apps.integrations.views.operations_mcp._record_oauth_security_event",
            side_effect=RuntimeError("oauth refresh audit unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/operations/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self.oauth_client.client_id,
                        "refresh_token": "test-refresh",
                        "resource": "http://testserver/operations/mcp/",
                    },
                )

        token.refresh_from_db()
        self.assertEqual(token.access_token_hash, before_access_hash)
        self.assertEqual(token.refresh_token_hash, before_refresh_hash)
        self.assertEqual(token.expires_at, before_access_expiry)
        self.assertEqual(
            token.refresh_expires_at,
            before_refresh_expiry,
        )

        old_access_response = self._call(bearer, "get_operations_context")
        self.assertEqual(old_access_response.status_code, 200)
        old_access = old_access_response.json()["result"]
        self.assertFalse(old_access["isError"])

        success = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.oauth_client.client_id,
                "refresh_token": "test-refresh",
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(success.status_code, 200)

    def test_operations_oauth_enforces_strong_pkce_and_no_cache(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.create()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )

        callback = "https://chatgpt.com/aip/callback"
        resource = "http://testserver/operations/mcp/"

        weak = self.client.get(
            "/operations/oauth/authorize",
            {
                "client_id": self.oauth_client.client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "code_challenge": "weak",
                "code_challenge_method": "S256",
                "scope": OPERATIONS_READ_SCOPE,
                "resource": resource,
            },
        )
        self.assertEqual(weak.status_code, 400)

        verifier = "p" * 64
        authorize = self.client.post(
            "/operations/oauth/authorize",
            data={
                "client_id": self.oauth_client.client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "code_challenge": pkce_s256(verifier),
                "code_challenge_method": "S256",
                "scope": OPERATIONS_READ_SCOPE,
                "resource": resource,
                "actor_mode": ROLE_ORGANIZATION_ADMIN,
            },
        )
        self.assertEqual(authorize.status_code, 302)
        code = parse_qs(urlparse(authorize["Location"]).query)["code"][0]

        weak_exchange = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.oauth_client.client_id,
                "code": code,
                "redirect_uri": callback,
                "code_verifier": "short",
                "resource": resource,
            },
        )
        self.assertEqual(weak_exchange.status_code, 400)
        self.assertEqual(
            weak_exchange["Cache-Control"],
            "no-store",
        )
        self.assertEqual(
            weak_exchange["Pragma"],
            "no-cache",
        )

        # A failed verifier must not consume the authorization code.
        success = self.client.post(
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
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success["Cache-Control"], "no-store")
        self.assertEqual(success["Pragma"], "no-cache")

        unsupported = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "password",
                "client_id": self.oauth_client.client_id,
            },
        )
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported["Cache-Control"], "no-store")
        self.assertEqual(unsupported["Pragma"], "no-cache")

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

        oauth_events = list(
            OperationsAuditEvent.objects.filter(
                actor=self.admin,
                organization=self.organization,
                tool_name__in=[
                    "oauth_authorize",
                    "oauth_token_issue",
                ],
            ).order_by("created_at")
        )
        self.assertEqual(
            [event.tool_name for event in oauth_events],
            ["oauth_authorize", "oauth_token_issue"],
        )
        for event in oauth_events:
            self.assertIn(
                OPERATIONS_READ_SCOPE,
                event.change_summary["scopes"],
            )
            self.assertNotIn(
                OPERATIONS_WRITE_SCOPE,
                event.change_summary["scopes"],
            )
        audit_blob = json.dumps(
            [
                {
                    "summary": event.change_summary,
                    "fingerprint": event.request_fingerprint,
                }
                for event in oauth_events
            ]
        )
        self.assertNotIn(body["access_token"], audit_blob)
        self.assertNotIn(body["refresh_token"], audit_blob)

        refresh = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.oauth_client.client_id,
                "refresh_token": body["refresh_token"],
                "resource": resource,
            },
        )
        self.assertEqual(refresh.status_code, 200)
        refreshed = refresh.json()
        self.assertNotEqual(
            refreshed["access_token"],
            body["access_token"],
        )
        refresh_event = OperationsAuditEvent.objects.get(
            actor=self.admin,
            organization=self.organization,
            tool_name="oauth_token_refresh",
        )
        refresh_blob = json.dumps(
            {
                "summary": refresh_event.change_summary,
                "fingerprint": refresh_event.request_fingerprint,
            }
        )
        self.assertNotIn(refreshed["access_token"], refresh_blob)
        self.assertNotIn(refreshed["refresh_token"], refresh_blob)

    def test_superadmin_policy_workspace_expands_legacy_capabilities(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_CRM_CONFIG_WRITE,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[
                CAP_CRM_CONFIG_WRITE,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
        )

        superadmin_session = SessionStore()
        set_authenticated_user(superadmin_session, self.superadmin)
        superadmin_session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = (
            superadmin_session.session_key
        )

        response = self.client.get(
            reverse(
                "superadmin-organization-detail",
                kwargs={"organization_id": self.organization.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        rows = {
            item["key"]: item
            for item in response.context["operations_capabilities"]
        }
        for capability in (
            CAP_PIPELINE_CONFIG_WRITE,
            CAP_STAGE_CONFIG_WRITE,
            CAP_ATTRIBUTE_CONFIG_WRITE,
            CAP_WORKFLOW_CONFIG_WRITE,
            CAP_CADENCE_CONFIG_WRITE,
            CAP_MESSAGING_CONFIG_WRITE,
        ):
            self.assertTrue(rows[capability]["allowed"])
            self.assertTrue(rows[capability]["approval_required"])

    def test_superadmin_policy_normalizes_duplicate_and_unknown_capabilities(self):
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
                    CAP_PIPELINE_CONFIG_WRITE,
                    CAP_PIPELINE_CONFIG_WRITE,
                    "unknown.capability",
                ],
                "approval_required_capabilities": [
                    CAP_PIPELINE_CONFIG_WRITE,
                    CAP_PIPELINE_CONFIG_WRITE,
                    CAP_ORGANIZATION_READ,
                    "unknown.capability",
                ],
            },
        )
        self.assertEqual(response.status_code, 302)

        policy = OperationsPolicy.objects.get(
            organization=self.organization
        )
        self.assertEqual(
            policy.allowed_capabilities,
            [
                CAP_ORGANIZATION_READ,
                CAP_PIPELINE_CONFIG_WRITE,
            ],
        )
        self.assertEqual(
            policy.approval_required_capabilities,
            [CAP_PIPELINE_CONFIG_WRITE],
        )

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

    def test_superadmin_dashboard_controls_revoke_org_session_and_end_support_context(self):
        org_token = OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=self.admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash("super-control-org-access"),
            refresh_token_hash=token_hash("super-control-org-refresh"),
            scope=f"{OPERATIONS_READ_SCOPE} {OPERATIONS_WRITE_SCOPE}",
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        support_token = OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            active_organization=self.organization,
            access_token_hash=token_hash("super-control-support-access"),
            refresh_token_hash=token_hash("super-control-support-refresh"),
            scope=f"{OPERATIONS_READ_SCOPE} {OPERATIONS_WRITE_SCOPE}",
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        support_session = OperationsSupportSession.objects.create(
            token=support_token,
            actor=self.superadmin,
            organization=self.organization,
            reason="Superadmin control test",
        )

        superadmin_session = SessionStore()
        set_authenticated_user(superadmin_session, self.superadmin)
        superadmin_session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = (
            superadmin_session.session_key
        )

        page = self.client.get(
            reverse(
                "superadmin-organization-detail",
                kwargs={"organization_id": self.organization.id},
            )
        )
        self.assertEqual(page.status_code, 200)
        body = page.content.decode("utf-8")
        self.assertIn("Organization Admin AI Sessions", body)
        self.assertIn("Open SHVYA Support Contexts", body)
        self.assertIn("Recent Operations Audit", body)
        self.assertIn(self.admin.name, body)
        self.assertIn("Superadmin control test", body)
        self.assertNotIn("super-control-org-access", body)
        self.assertNotIn("super-control-org-refresh", body)
        self.assertNotIn("super-control-support-access", body)
        self.assertNotIn("super-control-support-refresh", body)

        revoke = self.client.post(
            reverse(
                "superadmin-organization-operations-session-revoke",
                kwargs={
                    "organization_id": self.organization.id,
                    "token_id": org_token.id,
                },
            )
        )
        self.assertEqual(revoke.status_code, 302)
        org_token.refresh_from_db()
        self.assertIsNotNone(org_token.revoked_at)
        revoke_audit = OperationsAuditEvent.objects.get(
            organization=self.organization,
            tool_name="oauth_revoke_superadmin_dashboard",
            target_id=str(org_token.id),
        )
        self.assertEqual(revoke_audit.actor_id, self.superadmin.id)
        self.assertEqual(revoke_audit.role, ROLE_SUPERADMIN)

        ended = self.client.post(
            reverse(
                "superadmin-organization-operations-support-end",
                kwargs={
                    "organization_id": self.organization.id,
                    "session_id": support_session.id,
                },
            )
        )
        self.assertEqual(ended.status_code, 302)
        support_session.refresh_from_db()
        support_token.refresh_from_db()
        self.assertIsNotNone(support_session.ended_at)
        self.assertIsNone(support_token.active_organization_id)
        end_audit = OperationsAuditEvent.objects.get(
            organization=self.organization,
            tool_name="support_context_force_end",
            target_id=str(support_session.id),
        )
        self.assertEqual(end_audit.support_session_id, support_session.id)
        self.assertEqual(end_audit.actor_id, self.superadmin.id)

    def test_superadmin_operations_session_controls_are_tenant_scoped(self):
        foreign_token = OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=self.admin,
            organization=self.other_organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash("foreign-control-access"),
            refresh_token_hash=token_hash("foreign-control-refresh"),
            scope=OPERATIONS_READ_SCOPE,
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        foreign_support_token = OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            active_organization=self.other_organization,
            access_token_hash=token_hash("foreign-support-access"),
            refresh_token_hash=token_hash("foreign-support-refresh"),
            scope=OPERATIONS_READ_SCOPE,
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        foreign_support = OperationsSupportSession.objects.create(
            token=foreign_support_token,
            actor=self.superadmin,
            organization=self.other_organization,
            reason="Foreign tenant support",
        )

        superadmin_session = SessionStore()
        set_authenticated_user(superadmin_session, self.superadmin)
        superadmin_session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = (
            superadmin_session.session_key
        )

        token_response = self.client.post(
            reverse(
                "superadmin-organization-operations-session-revoke",
                kwargs={
                    "organization_id": self.organization.id,
                    "token_id": foreign_token.id,
                },
            )
        )
        self.assertEqual(token_response.status_code, 404)
        foreign_token.refresh_from_db()
        self.assertIsNone(foreign_token.revoked_at)

        support_response = self.client.post(
            reverse(
                "superadmin-organization-operations-support-end",
                kwargs={
                    "organization_id": self.organization.id,
                    "session_id": foreign_support.id,
                },
            )
        )
        self.assertEqual(support_response.status_code, 404)
        foreign_support.refresh_from_db()
        foreign_support_token.refresh_from_db()
        self.assertIsNone(foreign_support.ended_at)
        self.assertEqual(
            foreign_support_token.active_organization_id,
            self.other_organization.id,
        )

    def test_policy_disable_expires_pending_oauth_codes_before_reenable(self):
        policy = OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        verifier = "q" * 64
        resource = "http://testserver/operations/mcp/"
        callback = "https://chatgpt.com/aip/callback"
        old_raw_code = "old-pending-operations-code"
        old_code = OperationsOAuthAuthorizationCode.objects.create(
            client=self.oauth_client,
            actor=self.admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            code_hash=token_hash(old_raw_code),
            redirect_uri=callback,
            code_challenge=pkce_s256(verifier),
            scope=OPERATIONS_READ_SCOPE,
            granted_capabilities=[CAP_ORGANIZATION_READ],
            resource=resource,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        superadmin_session = SessionStore()
        set_authenticated_user(superadmin_session, self.superadmin)
        superadmin_session.create()
        self.client.cookies[get_session_cookie_name("superadmin")] = (
            superadmin_session.session_key
        )

        disabled = self.client.post(
            reverse(
                "superadmin-organization-operations-mcp-policy",
                kwargs={"organization_id": self.organization.id},
            ),
            {
                "allowed_capabilities": [CAP_ORGANIZATION_READ],
            },
        )
        self.assertEqual(disabled.status_code, 302)
        old_code.refresh_from_db()
        self.assertLessEqual(old_code.expires_at, timezone.now())

        enabled = self.client.post(
            reverse(
                "superadmin-organization-operations-mcp-policy",
                kwargs={"organization_id": self.organization.id},
            ),
            {
                "organization_admin_enabled": "on",
                "allowed_capabilities": [CAP_ORGANIZATION_READ],
            },
        )
        self.assertEqual(enabled.status_code, 302)
        policy.refresh_from_db()
        self.assertTrue(policy.organization_admin_enabled)

        old_exchange = self.client.post(
            reverse("shvya-operations-oauth-token"),
            {
                "grant_type": "authorization_code",
                "client_id": self.oauth_client.client_id,
                "code": old_raw_code,
                "redirect_uri": callback,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        self.assertEqual(old_exchange.status_code, 400)

        fresh_raw_code = "fresh-operations-code"
        OperationsOAuthAuthorizationCode.objects.create(
            client=self.oauth_client,
            actor=self.admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            code_hash=token_hash(fresh_raw_code),
            redirect_uri=callback,
            code_challenge=pkce_s256(verifier),
            scope=OPERATIONS_READ_SCOPE,
            granted_capabilities=[CAP_ORGANIZATION_READ],
            resource=resource,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        fresh_exchange = self.client.post(
            reverse("shvya-operations-oauth-token"),
            {
                "grant_type": "authorization_code",
                "client_id": self.oauth_client.client_id,
                "code": fresh_raw_code,
                "redirect_uri": callback,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        self.assertEqual(fresh_exchange.status_code, 200)

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

        old_token_response = self._call(bearer, "get_operations_context")
        self.assertEqual(old_token_response.status_code, 401)
        old_token_result = old_token_response.json()["result"]
        self.assertTrue(old_token_result["isError"])
        self.assertIn("mcp/www_authenticate", old_token_result["_meta"])

    def test_invalid_refresh_authority_commits_permanent_grant_revocation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        token = OperationsOAuthToken.objects.get(actor=self.admin)

        self.admin.role = User.Role.AGENT
        self.admin.save(update_fields=["role", "updated_at"])

        denied = self.client.post(
            reverse("shvya-operations-oauth-token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.oauth_client.client_id,
                "refresh_token": "test-refresh",
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(denied.status_code, 400)

        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        revoke_audit = OperationsAuditEvent.objects.get(
            actor=self.admin,
            organization=self.organization,
            tool_name="oauth_auto_revoke",
            target_id=str(token.id),
        )
        self.assertEqual(
            revoke_audit.change_summary["reason_code"],
            "live_authority_invalid",
        )
        self.assertNotIn(
            "test-access",
            json.dumps(revoke_audit.change_summary),
        )
        self.assertNotIn(
            "test-refresh",
            json.dumps(revoke_audit.change_summary),
        )

        self.admin.role = User.Role.ADMIN
        self.admin.save(update_fields=["role", "updated_at"])

        still_denied = self.client.post(
            reverse("shvya-operations-oauth-token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.oauth_client.client_id,
                "refresh_token": "test-refresh",
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(still_denied.status_code, 400)

    def test_oauth_refresh_rotates_tokens_without_extending_grant_lifetime(self):
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
        token = OperationsOAuthToken.objects.get(actor=self.admin)
        original_refresh_expiry = token.refresh_expires_at

        response = self.client.post(
            reverse("shvya-operations-oauth-token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.oauth_client.client_id,
                "refresh_token": "test-refresh",
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotEqual(body["access_token"], bearer)
        self.assertNotEqual(body["refresh_token"], "test-refresh")

        token.refresh_from_db()
        self.assertEqual(
            token.refresh_expires_at,
            original_refresh_expiry,
        )

        old_access_response = self._call(bearer, "get_operations_context")
        self.assertEqual(old_access_response.status_code, 401)
        old_access = old_access_response.json()["result"]
        self.assertTrue(old_access["isError"])
        self.assertIn("mcp/www_authenticate", old_access["_meta"])

        old_refresh = self.client.post(
            reverse("shvya-operations-oauth-token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.oauth_client.client_id,
                "refresh_token": "test-refresh",
                "resource": "http://testserver/operations/mcp/",
            },
        )
        self.assertEqual(old_refresh.status_code, 400)

        fresh_access = self._result(
            self._call(
                body["access_token"],
                "get_operations_context",
            )
        )
        self.assertFalse(fresh_access["isError"])

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

        repeated = self.client.post(
            reverse("shvya-operations-oauth-revoke"),
            {
                "token": bearer,
                "token_type_hint": "access_token",
            },
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(
            OperationsAuditEvent.objects.filter(
                actor=self.superadmin,
                tool_name="oauth_revoke",
            ).count(),
            1,
        )

        response = self._call(bearer, "get_operations_context")
        self.assertEqual(response.status_code, 401)
        result = response.json()["result"]
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

        response = self._call(bearer, "get_operations_context")
        self.assertEqual(response.status_code, 401)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])

    def test_oauth_revocation_does_not_disclose_unknown_token_state(self):
        response = self.client.post(
            reverse("shvya-operations-oauth-revoke"),
            {"token": "unknown-token-value"},
        )
        self.assertEqual(response.status_code, 200)

    def test_live_org_admin_authority_loss_permanently_revokes_operations_grant(self):
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
        token = OperationsOAuthToken.objects.get(actor=self.admin)

        self.admin.role = User.Role.AGENT
        self.admin.save(update_fields=["role", "updated_at"])

        blocked_response = self._call(
            bearer,
            "get_operations_context",
        )
        self.assertEqual(blocked_response.status_code, 401)
        blocked = blocked_response.json()["result"]
        self.assertTrue(blocked["isError"])
        self.assertIn("mcp/www_authenticate", blocked["_meta"])

        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)

        self.admin.role = User.Role.ADMIN
        self.admin.save(update_fields=["role", "updated_at"])
        still_blocked_response = self._call(
            bearer,
            "get_operations_context",
        )
        self.assertEqual(still_blocked_response.status_code, 401)
        still_blocked = still_blocked_response.json()["result"]
        self.assertTrue(still_blocked["isError"])
        self.assertIn("mcp/www_authenticate", still_blocked["_meta"])

    def test_live_superadmin_authority_loss_revokes_grant_and_closes_support_context(self):
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
                    "reason": "Test Superadmin authority revocation",
                },
            )
        )
        self.assertFalse(selected["isError"])
        token = OperationsOAuthToken.objects.get(actor=self.superadmin)
        support = OperationsSupportSession.objects.get(
            token=token,
            organization=self.organization,
            ended_at__isnull=True,
        )

        self.superadmin.is_superuser = False
        self.superadmin.save(update_fields=["is_superuser", "updated_at"])

        blocked_response = self._call(
            bearer,
            "get_operations_context",
        )
        self.assertEqual(blocked_response.status_code, 401)
        blocked = blocked_response.json()["result"]
        self.assertTrue(blocked["isError"])
        self.assertIn("mcp/www_authenticate", blocked["_meta"])

        token.refresh_from_db()
        support.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        self.assertIsNotNone(support.ended_at)
        revoke_audit = OperationsAuditEvent.objects.get(
            actor=self.superadmin,
            organization=self.organization,
            tool_name="oauth_auto_revoke",
            target_id=str(token.id),
        )
        self.assertEqual(
            revoke_audit.support_session_id,
            support.id,
        )
        self.assertTrue(
            revoke_audit.change_summary[
                "support_session_closed"
            ]
        )

        self.superadmin.is_superuser = True
        self.superadmin.save(update_fields=["is_superuser", "updated_at"])
        still_blocked_response = self._call(
            bearer,
            "get_operations_context",
        )
        self.assertEqual(still_blocked_response.status_code, 401)
        still_blocked = still_blocked_response.json()["result"]
        self.assertTrue(still_blocked["isError"])
        self.assertIn("mcp/www_authenticate", still_blocked["_meta"])

    def test_org_admin_token_is_rejected_while_policy_disabled(self):
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        response = self._call(bearer, "get_operations_context")
        self.assertEqual(response.status_code, 401)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])
