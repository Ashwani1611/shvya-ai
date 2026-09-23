# ruff: noqa: F403,F405
"""Domain-focused coverage split from the historical Operations MCP suite."""

from apps.integrations.tests.operations_mcp_test_base import *


class TestOperationsMCPProtocol(OperationsMCPBase):
    def test_unauthenticated_tool_call_returns_oauth_401_challenge(self):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "oauth-required",
                    "method": "tools/call",
                    "params": {
                        "name": "get_operations_context",
                        "arguments": {},
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response)
        challenge = response["WWW-Authenticate"]
        self.assertIn("Bearer ", challenge)
        self.assertIn(
            'resource_metadata="http://testserver/.well-known/oauth-protected-resource/operations/mcp/"',
            challenge,
        )
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])
        self.assertEqual(
            result["_meta"]["mcp/www_authenticate"],
            [challenge],
        )

    def test_expired_bearer_returns_oauth_401_challenge(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        OperationsOAuthToken.objects.filter(
            access_token_hash=token_hash(bearer)
        ).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        response = self._call(
            bearer,
            "get_operations_context",
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])

    def test_operations_related_migration_graphs_have_single_leaf(self):
        conflicts = MigrationLoader(
            None,
            ignore_no_migrations=True,
        ).detect_conflicts()
        self.assertNotIn("integrations", conflicts)
        self.assertNotIn("channels", conflicts)

    def test_operations_tool_schema_rejects_non_object_arguments(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        result = self._result(
            self._call(
                bearer,
                "get_operations_context",
                "not-an-object",
            )
        )
        self.assertTrue(result["isError"])
        self.assertIn(
            "must be an object",
            result["structuredContent"]["error"],
        )

    def test_operations_tool_schema_rejects_unadvertised_arguments(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        result = self._result(
            self._call(
                bearer,
                "get_operations_context",
                {"unexpected": "value"},
            )
        )
        self.assertTrue(result["isError"])
        self.assertIn(
            "unexpected field",
            result["structuredContent"]["error"],
        )

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

    def test_operations_tools_advertise_least_privilege_oauth_scopes(self):
        result = self._list_tools()
        tools = {
            item["name"]: item
            for item in result["tools"]
        }
        for tool in tools.values():
            self.assertEqual(
                tool["_meta"]["securitySchemes"],
                tool["securitySchemes"],
            )
        for name in (
            "find_leads",
            "get_lead_snapshot",
            "get_ai_diagnostics",
            "get_runtime_health",
            "get_organization_configuration",
            "get_ai_configuration",
            "get_knowledge_health",
            "get_messaging_automation_settings",
        ):
            self.assertIn(name, tools)
            schemes = tools[name]["securitySchemes"]
            self.assertEqual(schemes[0]["type"], "oauth2")
            self.assertIn(
                OPERATIONS_READ_SCOPE,
                schemes[0]["scopes"],
            )
            self.assertNotIn(
                OPERATIONS_WRITE_SCOPE,
                schemes[0]["scopes"],
            )
            self.assertNotIn(
                "diagnostics.read",
                schemes[0]["scopes"],
            )

        for name in (
            "move_lead_stage",
            "update_ai_configuration",
            "upsert_workflow_configuration",
            "update_messaging_automation_settings",
        ):
            schemes = tools[name]["securitySchemes"]
            self.assertIn(
                OPERATIONS_READ_SCOPE,
                schemes[0]["scopes"],
            )
            self.assertIn(
                OPERATIONS_WRITE_SCOPE,
                schemes[0]["scopes"],
            )

        for name in (
            "select_organization_context",
            "clear_organization_context",
        ):
            schemes = tools[name]["securitySchemes"]
            self.assertIn(
                OPERATIONS_READ_SCOPE,
                schemes[0]["scopes"],
            )
            self.assertNotIn(
                OPERATIONS_WRITE_SCOPE,
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
            "update_messaging_automation_settings",
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
            self.assertFalse(
                tools[name]["annotations"]["readOnlyHint"]
            )
            scopes = tools[name]["securitySchemes"][0]["scopes"]
            self.assertIn(OPERATIONS_READ_SCOPE, scopes)
            self.assertNotIn(OPERATIONS_WRITE_SCOPE, scopes)

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

    def test_legacy_policy_umbrellas_expand_but_granular_controls_can_isolate_tools(self):
        policy = OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        legacy_names = {
            item["name"]
            for item in self._list_tools(bearer)["tools"]
        }
        self.assertIn(
            "upsert_workflow_configuration",
            legacy_names,
        )
        self.assertIn(
            "upsert_cadence_configuration",
            legacy_names,
        )
        self.assertIn(
            "add_cadence_step",
            legacy_names,
        )
        self.assertIn(
            "update_messaging_automation_settings",
            legacy_names,
        )

        context = self._result(
            self._call(
                bearer,
                "get_operations_context",
                {},
            )
        )
        self.assertFalse(context["isError"])
        for capability in (
            CAP_WORKFLOW_CONFIG_WRITE,
            CAP_CADENCE_CONFIG_WRITE,
            CAP_MESSAGING_CONFIG_WRITE,
        ):
            self.assertIn(
                capability,
                context["structuredContent"]["capabilities"],
            )

        policy.allowed_capabilities = [
            CAP_ORGANIZATION_READ,
            CAP_WORKFLOW_CONFIG_WRITE,
        ]
        policy.approval_required_capabilities = [
            CAP_WORKFLOW_CONFIG_WRITE,
        ]
        policy.save(
            update_fields=[
                "allowed_capabilities",
                "approval_required_capabilities",
                "updated_at",
            ]
        )

        workflow_only_names = {
            item["name"]
            for item in self._list_tools(bearer)["tools"]
        }
        self.assertIn(
            "upsert_workflow_configuration",
            workflow_only_names,
        )
        self.assertNotIn(
            "upsert_cadence_configuration",
            workflow_only_names,
        )
        self.assertNotIn(
            "add_cadence_step",
            workflow_only_names,
        )
        self.assertNotIn(
            "update_messaging_automation_settings",
            workflow_only_names,
        )

        denied_cadence = self._result(
            self._call(
                bearer,
                "upsert_cadence_configuration",
                {
                    "data": {
                        "name": "Denied Cadence",
                    },
                    "reason": "Test granular automation denial",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(denied_cadence["isError"])
        self.assertEqual(
            denied_cadence["structuredContent"]["status"],
            "SUPERADMIN_REQUIRED",
        )

        policy.allowed_capabilities = [
            CAP_ORGANIZATION_READ,
            CAP_PIPELINE_CONFIG_WRITE,
        ]
        policy.approval_required_capabilities = [
            CAP_PIPELINE_CONFIG_WRITE,
        ]
        policy.save(
            update_fields=[
                "allowed_capabilities",
                "approval_required_capabilities",
                "updated_at",
            ]
        )

        stale_grant_names = {
            item["name"]
            for item in self._list_tools(bearer)["tools"]
        }
        self.assertNotIn(
            "upsert_pipeline_configuration",
            stale_grant_names,
        )
        stale_context = self._result(
            self._call(
                bearer,
                "get_operations_context",
                {},
            )
        )
        self.assertFalse(stale_context["isError"])
        self.assertIn(
            CAP_PIPELINE_CONFIG_WRITE,
            stale_context["structuredContent"]["policy_capabilities"],
        )
        self.assertNotIn(
            CAP_PIPELINE_CONFIG_WRITE,
            stale_context["structuredContent"]["granted_capabilities"],
        )

        OperationsOAuthToken.objects.filter(
            actor=self.admin,
        ).delete()
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        pipeline_only_names = {
            item["name"]
            for item in self._list_tools(bearer)["tools"]
        }
        self.assertIn(
            "upsert_pipeline_configuration",
            pipeline_only_names,
        )
        self.assertNotIn(
            "upsert_stage_configuration",
            pipeline_only_names,
        )
        self.assertNotIn(
            "upsert_attribute_configuration",
            pipeline_only_names,
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

    def test_policy_expansion_requires_fresh_oauth_but_reduction_is_immediate(self):
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

        before = {
            item["name"]
            for item in self._list_tools(bearer)["tools"]
        }
        self.assertNotIn("move_lead_stage", before)

        policy.allowed_capabilities = [
            CAP_ORGANIZATION_READ,
            CAP_LEAD_STAGE_WRITE,
        ]
        policy.save(
            update_fields=[
                "allowed_capabilities",
                "updated_at",
            ]
        )

        expanded_without_reauth = {
            item["name"]
            for item in self._list_tools(bearer)["tools"]
        }
        self.assertNotIn(
            "move_lead_stage",
            expanded_without_reauth,
        )
        context = self._result(
            self._call(
                bearer,
                "get_operations_context",
                {},
            )
        )
        self.assertIn(
            CAP_LEAD_STAGE_WRITE,
            context["structuredContent"][
                "policy_capabilities"
            ],
        )
        self.assertNotIn(
            CAP_LEAD_STAGE_WRITE,
            context["structuredContent"][
                "granted_capabilities"
            ],
        )
        self.assertNotIn(
            CAP_LEAD_STAGE_WRITE,
            context["structuredContent"]["capabilities"],
        )

        denied = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(
                        self.review_stage.id
                    ),
                    "reason": "Try newly enabled capability",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(denied["isError"])
        self.assertEqual(
            denied["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "Fresh SHVYA authorization",
            denied["structuredContent"]["error"],
        )

        OperationsOAuthToken.objects.all().delete()
        fresh_bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        after_reauth = {
            item["name"]
            for item in self._list_tools(
                fresh_bearer
            )["tools"]
        }
        self.assertIn(
            "move_lead_stage",
            after_reauth,
        )
        self.assertIn(
            "repair_qualification_stage",
            after_reauth,
        )

        policy.allowed_capabilities = [
            CAP_ORGANIZATION_READ,
        ]
        policy.save(
            update_fields=[
                "allowed_capabilities",
                "updated_at",
            ]
        )
        reduced = {
            item["name"]
            for item in self._list_tools(
                fresh_bearer
            )["tools"]
        }
        self.assertNotIn("move_lead_stage", reduced)
        reduced_call = self._result(
            self._call(
                fresh_bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(
                        self.review_stage.id
                    ),
                    "reason": "Try capability after policy reduction",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(reduced_call["isError"])
        self.assertEqual(
            reduced_call["structuredContent"]["status"],
            "SUPERADMIN_REQUIRED",
        )

    def test_real_oauth_reauthorization_is_required_for_policy_expansion(self):
        policy = OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        dashboard_session = SessionStore()
        set_authenticated_user(
            dashboard_session,
            self.admin,
        )
        dashboard_session.create()
        self.client.cookies[
            get_session_cookie_name("dashboard")
        ] = dashboard_session.session_key

        callback = "https://chatgpt.com/aip/callback"
        resource = "http://testserver/operations/mcp/"

        first_verifier = "g" * 64
        first_authorize = self.client.post(
            "/operations/oauth/authorize",
            data={
                "client_id": self.oauth_client.client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "code_challenge": pkce_s256(
                    first_verifier
                ),
                "code_challenge_method": "S256",
                "scope": (
                    f"{OPERATIONS_READ_SCOPE} "
                    f"{OPERATIONS_WRITE_SCOPE}"
                ),
                "resource": resource,
                "actor_mode": ROLE_ORGANIZATION_ADMIN,
            },
        )
        self.assertEqual(first_authorize.status_code, 302)
        first_code = parse_qs(
            urlparse(
                first_authorize["Location"]
            ).query
        )["code"][0]
        first_token_response = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.oauth_client.client_id,
                "code": first_code,
                "redirect_uri": callback,
                "code_verifier": first_verifier,
                "resource": resource,
            },
        )
        self.assertEqual(
            first_token_response.status_code,
            200,
        )
        first_body = first_token_response.json()
        first_access = first_body["access_token"]
        self.assertEqual(
            first_body["scope"],
            OPERATIONS_READ_SCOPE,
        )
        self.assertIn(
            CAP_ORGANIZATION_READ,
            first_body["granted_capabilities"],
        )
        self.assertNotIn(
            CAP_LEAD_STAGE_WRITE,
            first_body["granted_capabilities"],
        )

        policy.allowed_capabilities = [
            CAP_ORGANIZATION_READ,
            CAP_LEAD_STAGE_WRITE,
        ]
        policy.save(
            update_fields=[
                "allowed_capabilities",
                "updated_at",
            ]
        )

        old_names = {
            item["name"]
            for item in self._list_tools(
                first_access
            )["tools"]
        }
        self.assertNotIn(
            "move_lead_stage",
            old_names,
        )

        second_verifier = "h" * 64
        second_authorize = self.client.post(
            "/operations/oauth/authorize",
            data={
                "client_id": self.oauth_client.client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "code_challenge": pkce_s256(
                    second_verifier
                ),
                "code_challenge_method": "S256",
                "scope": (
                    f"{OPERATIONS_READ_SCOPE} "
                    f"{OPERATIONS_WRITE_SCOPE}"
                ),
                "resource": resource,
                "actor_mode": ROLE_ORGANIZATION_ADMIN,
            },
        )
        self.assertEqual(
            second_authorize.status_code,
            302,
        )
        second_code = parse_qs(
            urlparse(
                second_authorize["Location"]
            ).query
        )["code"][0]
        second_token_response = self.client.post(
            "/operations/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.oauth_client.client_id,
                "code": second_code,
                "redirect_uri": callback,
                "code_verifier": second_verifier,
                "resource": resource,
            },
        )
        self.assertEqual(
            second_token_response.status_code,
            200,
        )
        second_body = second_token_response.json()
        second_access = second_body["access_token"]
        self.assertIn(
            OPERATIONS_WRITE_SCOPE,
            second_body["scope"].split(),
        )
        self.assertIn(
            CAP_LEAD_STAGE_WRITE,
            second_body["granted_capabilities"],
        )

        new_names = {
            item["name"]
            for item in self._list_tools(
                second_access
            )["tools"]
        }
        self.assertIn(
            "move_lead_stage",
            new_names,
        )
        self.assertIn(
            "repair_qualification_stage",
            new_names,
        )

        old_context = self._result(
            self._call(
                first_access,
                "get_operations_context",
                {},
            )
        )
        self.assertIn(
            CAP_LEAD_STAGE_WRITE,
            old_context["structuredContent"][
                "policy_capabilities"
            ],
        )
        self.assertNotIn(
            CAP_LEAD_STAGE_WRITE,
            old_context["structuredContent"][
                "granted_capabilities"
            ],
        )
        self.assertNotIn(
            CAP_LEAD_STAGE_WRITE,
            old_context["structuredContent"][
                "capabilities"
            ],
        )

    def test_read_only_oauth_tokens_do_not_discover_write_tools(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
                CAP_AI_CONFIG_WRITE,
                CAP_CRM_CONFIG_WRITE,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
        )

        org_bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        org_names = {
            item["name"]
            for item in self._list_tools(org_bearer)["tools"]
        }
        self.assertIn("get_operations_context", org_names)
        self.assertIn("get_organization_configuration", org_names)
        self.assertNotIn("move_lead_stage", org_names)
        self.assertNotIn("update_ai_configuration", org_names)
        self.assertNotIn("upsert_pipeline_configuration", org_names)

        OperationsOAuthToken.objects.all().delete()
        super_bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        super_names = {
            item["name"]
            for item in self._list_tools(super_bearer)["tools"]
        }
        self.assertIn("list_organizations", super_names)
        self.assertIn("get_operations_context", super_names)
        self.assertIn("select_organization_context", super_names)
        self.assertIn("clear_organization_context", super_names)
        self.assertNotIn("move_lead_stage", super_names)
        self.assertNotIn("upsert_workflow_configuration", super_names)

    def test_superadmin_organization_discovery_reports_truncation(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        result = self._result(
            self._call(
                bearer,
                "list_organizations",
                {"limit": 1},
            )
        )
        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["organizations_returned"], 1)
        self.assertGreaterEqual(data["organization_count"], 2)
        self.assertTrue(data["organizations_truncated"])

    def test_read_only_superadmin_can_select_context_but_cannot_mutate_customer_state(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Read-only customer diagnostic review",
                },
            )
        )
        self.assertFalse(selected["isError"])
        self.assertEqual(
            selected["structuredContent"]["organization"]["id"],
            str(self.organization.id),
        )

        config = self._result(
            self._call(
                bearer,
                "get_organization_configuration",
                {},
            )
        )
        self.assertFalse(config["isError"])

        context = self._result(
            self._call(
                bearer,
                "get_operations_context",
                {},
            )
        )
        self.assertFalse(context["isError"])
        self.assertNotIn(
            CAP_LEAD_STAGE_WRITE,
            context["structuredContent"]["capabilities"],
        )
        self.assertIn(
            CAP_LEAD_STAGE_WRITE,
            context["structuredContent"]["policy_capabilities"],
        )
        self.assertEqual(
            context["structuredContent"]["oauth_scopes"],
            [OPERATIONS_READ_SCOPE],
        )

        blocked = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "Attempt mutation from read-only grant",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "Fresh SHVYA authorization",
            blocked["structuredContent"]["error"],
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

        cleared = self._result(
            self._call(
                bearer,
                "clear_organization_context",
                {
                    "reason": "Finish read-only diagnostic review",
                },
            )
        )
        self.assertFalse(cleared["isError"])
        self.assertEqual(
            cleared["structuredContent"]["status"],
            "cleared",
        )

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
