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
            "validate_calendar_configuration",
            "upsert_calendar_configuration",
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
            "get_team_settings",
            "upsert_team_settings",
        }
        catalog_names = {item["name"] for item in TOOL_DEFINITIONS}
        self.assertTrue(names <= catalog_names)
        for name in names:
            schema = TOOL_INPUT_SCHEMAS[name]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema.get("additionalProperties", True))

    def test_production_mutations_advertise_write_capabilities(self):
        self.assertEqual(TOOL_CAPABILITIES["upsert_calendar_configuration"], "calendar.config.write")
        self.assertEqual(TOOL_CAPABILITIES["disconnect_integration"], "integration.lifecycle.write")
        self.assertEqual(TOOL_CAPABILITIES["upsert_commitment"], "operations.task.write")

    def test_discovery_excludes_voice_and_vault(self):
        class Actor:
            role = "SHVYA_SUPERADMIN"
            active_organization = None
            organization = None
            granted_capabilities = []
            scopes = {"operations.read"}

        payload = capability_discovery(identity=Actor(), tools=[])
        names = {item["name"] for item in payload["unsupported_features"]}
        self.assertEqual(names, {"Voice Agent", "Vault"})

    def test_discovery_payload_is_json_serializable(self):
        class Actor:
            role = "SHVYA_SUPERADMIN"
            active_organization = None
            organization = None
            granted_capabilities = []
            scopes = {"operations.read"}

        payload = capability_discovery(identity=Actor(), tools=[{"name": "get_operations_context", "capability": None, "securitySchemes": []}])
        json.dumps(payload)
