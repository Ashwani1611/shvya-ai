"""Fast contract tests for the capability expansion (no database required)."""

import json

from django.test import SimpleTestCase

from apps.integrations.operations.capabilities import capability_discovery
from apps.integrations.operations.tool_catalog import TOOL_CAPABILITIES, TOOL_DEFINITIONS, TOOL_INPUT_SCHEMAS


class OperationsCapabilityExpansionContractTests(SimpleTestCase):
    def test_new_tools_have_closed_object_schemas(self):
        names = {
            "get_capability_discovery",
            "get_production_trace",
            "get_calendar_configuration",
            "get_calendar_available_slots",
            "get_calendar_setup_readiness",
            "validate_calendar_configuration",
            "create_calendar_page",
            "upsert_calendar_configuration",
            "upsert_calendar_reminder",
            "verify_booking",
            "update_booking_status",
            "reschedule_booking",
            "prepare_account_onboarding",
            "list_industry_playbooks",
            "get_integration_lifecycle",
            "disconnect_integration",
            "validate_cadence_batch",
            "run_acceptance_suite",
            "list_commitments",
            "upsert_commitment",
            "list_sales_templates",
            "upsert_sales_template_branding",
            "get_team_settings",
            "upsert_team_settings",
        }
        catalog_names = {item["name"] for item in TOOL_DEFINITIONS}
        self.assertTrue(names <= catalog_names)
        for name in names:
            schema = TOOL_INPUT_SCHEMAS[name]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema.get("additionalProperties", True))

    def test_calendar_read_tools_do_not_grant_write_capabilities(self):
        for name in ("get_calendar_available_slots", "get_calendar_setup_readiness"):
            self.assertEqual(TOOL_CAPABILITIES[name], "organization.read")
            self.assertFalse(TOOL_INPUT_SCHEMAS[name].get("additionalProperties", True))
        self.assertEqual(TOOL_INPUT_SCHEMAS["get_calendar_available_slots"]["required"], ["page_id", "date"])

    def test_production_mutations_advertise_write_capabilities(self):
        self.assertEqual(TOOL_CAPABILITIES["upsert_sales_template_branding"], "sales.template.write")
        self.assertEqual(TOOL_CAPABILITIES["create_calendar_page"], "calendar.config.write")
        self.assertEqual(TOOL_CAPABILITIES["upsert_calendar_reminder"], "calendar.config.write")
        self.assertEqual(TOOL_CAPABILITIES["upsert_calendar_configuration"], "calendar.config.write")
        self.assertEqual(TOOL_CAPABILITIES["disconnect_integration"], "integration.lifecycle.write")
        self.assertEqual(TOOL_CAPABILITIES["upsert_commitment"], "operations.task.write")

    def test_discovery_includes_native_vault_scope(self):
        class Actor:
            role = "SHVYA_SUPERADMIN"
            active_organization = None
            organization = None
            granted_capabilities = []
            scopes = {"operations.read"}

        payload = capability_discovery(identity=Actor(), tools=[])
        names = {item["name"] for item in payload["unsupported_features"]}
        self.assertEqual(names, {"Voice Agent"})

    def test_discovery_explains_new_permission_missing_from_old_oauth_grant(self):
        class Actor:
            role = "SHVYA_SUPERADMIN"
            active_organization = None
            organization = None
            granted_capabilities = ["organization.read"]
            scopes = {"operations.read", "operations.write"}

        tools = [{
            "name": "upsert_sales_template_branding",
            "capability": "sales.template.write",
            "securitySchemes": [{"type": "oauth2", "scopes": ["operations.write"]}],
        }]
        result = capability_discovery(identity=Actor(), tools=tools)
        self.assertTrue(result["oauth_reauthorization_required"])
        self.assertIn("sales.template.write", result["missing_oauth_grants"])
        self.assertEqual(result["tools"][0]["unavailable_reason"], "oauth_reauthorization_required")

    def test_discovery_payload_is_json_serializable(self):
        class Actor:
            role = "SHVYA_SUPERADMIN"
            active_organization = None
            organization = None
            granted_capabilities = []
            scopes = {"operations.read"}

        payload = capability_discovery(identity=Actor(), tools=[{"name": "get_operations_context", "capability": None, "securitySchemes": []}])
        json.dumps(payload)

    def test_discovery_does_not_offer_unscoped_writes(self):
        class Actor:
            role = "SHVYA_SUPERADMIN"
            active_organization = None
            organization = None
            granted_capabilities = ["organization.create", "vault.write"]
            scopes = {"operations.read"}

        tools = [{"name": item["name"], "capability": TOOL_CAPABILITIES.get(item["name"]),
                  "securitySchemes": item.get("securitySchemes", [])} for item in TOOL_DEFINITIONS]
        rows = {item["name"]: item for item in capability_discovery(identity=Actor(), tools=tools)["tools"]}
        self.assertFalse(rows["create_organization_account"]["available"])
        self.assertFalse(rows["create_vault_workspace"]["available"])
        # Provisioning still requires approval even before a tenant is selected.
        self.assertTrue(rows["create_organization_account"]["approval_required"])

    def test_expanded_powers_are_explicitly_granted_and_sensitive_actions_keep_approval(self):
        from apps.integrations.operations_policy import (
            DEFAULT_ORG_CAPABILITIES, ALWAYS_APPROVAL_CAPABILITIES,
            SUPERADMIN_ONLY_CAPABILITIES, approval_required,
        )
        expanded = {"organization.create", "lead.read", "lead.create", "lead.write", "lead.import",
                    "vault.read", "vault.write", "channel.group.read", "channel.group.send", "ai.flow_testing.write"}
        self.assertFalse(expanded.intersection(DEFAULT_ORG_CAPABILITIES))
        self.assertIn("organization.create", SUPERADMIN_ONLY_CAPABILITIES)
        self.assertTrue({"channel.group.send", "ai.flow_testing.write"} <= ALWAYS_APPROVAL_CAPABILITIES)
        for capability in ALWAYS_APPROVAL_CAPABILITIES:
            self.assertTrue(approval_required(role="ORGANIZATION_ADMIN", organization=None, capability=capability))

    def test_group_sends_and_billable_tests_cannot_be_hidden_in_configuration_plans(self):
        from apps.integrations.operations.configuration.common import ALLOWED_PLAN_TOOLS
        self.assertFalse({"send_hosted_whatsapp_group_message", "run_ai_flow_test_turn",
                          "create_ai_flow_test_run"}.intersection(ALLOWED_PLAN_TOOLS))

    def test_legacy_facade_preserves_focused_module_import_identity(self):
        from apps.integrations import operations_tools
        from apps.integrations.operations.tools import knowledge
        before = knowledge.__package__
        operations_tools._sync_facade_overrides()
        self.assertEqual(knowledge.__package__, before)
        self.assertEqual(knowledge.__package__, "apps.integrations.operations.tools")
