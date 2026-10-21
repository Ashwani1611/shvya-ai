from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.crm.models import AttributeDefinition
from apps.integrations.operations.tools.attribute_config import (
    _attribute_validation_message,
    upsert_attribute_configuration,
)
from apps.integrations.operations_tools import OperationsToolError
from apps.organizations.models import Organization
from services.crm.attribute_service import (
    MAX_CUSTOM_ATTRIBUTES,
    create_attribute_definition,
)


class OperationsMCPAttributeConfigurationRegressionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="AKC MCP Attribute Test")
        self.identity = SimpleNamespace(role="ORGANIZATION_ADMIN")

    def _dry_run(self, data):
        with (
            patch(
                "apps.integrations.operations.tools.attribute_config._organization_for",
                return_value=self.organization,
            ),
            patch(
                "apps.integrations.operations.tools.attribute_config._write_gate",
                return_value=(True, "Regression test"),
            ),
            patch(
                "apps.integrations.operations.tools.attribute_config.approval_required",
                return_value=False,
            ),
        ):
            return upsert_attribute_configuration(
                identity=self.identity,
                arguments={"data": data},
            )

    def test_boolean_currency_and_select_aliases_are_normalized(self):
        boolean = self._dry_run(
            {
                "name": "Financing Required",
                "field_type": "boolean",
                "description": "Whether financing is required.",
            }
        )
        self.assertEqual(boolean.data["after"]["field_type"], "option")
        self.assertEqual(boolean.data["after"]["options"], ["Yes", "No"])

        currency = self._dry_run(
            {
                "name": "Proposal Amount",
                "field_type": "currency",
                "description": "Proposal amount.",
            }
        )
        self.assertEqual(currency.data["after"]["field_type"], "numeric")

        select = self._dry_run(
            {
                "name": "Lead Temperature",
                "field_type": "select",
                "options": ["Hot", "Warm", "Cold"],
            }
        )
        self.assertEqual(select.data["after"]["field_type"], "option")
        self.assertEqual(select.data["after"]["options"], ["Hot", "Warm", "Cold"])

    def test_option_validation_identifies_the_offending_field(self):
        with self.assertRaises(OperationsToolError) as context:
            self._dry_run(
                {
                    "name": "Customer Type",
                    "field_type": "select",
                    "options": [],
                }
            )
        message = str(context.exception)
        self.assertIn("Attribute configuration validation failed:", message)
        self.assertIn("options:", message)
        self.assertIn("at least one option", message)

    def test_unsupported_type_is_explicit_instead_of_generic(self):
        with self.assertRaises(OperationsToolError) as context:
            self._dry_run(
                {
                    "name": "Unsupported Field",
                    "field_type": "multi_select",
                }
            )
        message = str(context.exception)
        self.assertIn("Unsupported attribute field_type", message)
        self.assertIn("multi_select", message)
        self.assertIn("Supported canonical types", message)

    def test_validation_message_preserves_safe_field_context(self):
        error = ValidationError(
            {
                "name": ["Attribute name is required."],
                "field_type": ["Invalid attribute type."],
            }
        )
        message = _attribute_validation_message(error)
        self.assertIn("name: Attribute name is required.", message)
        self.assertIn("field_type: Invalid attribute type.", message)

    def test_realistic_akc_setup_can_create_more_than_thirty_eight_attributes(self):
        self.assertGreaterEqual(MAX_CUSTOM_ATTRIBUTES, 38)
        for index in range(38):
            create_attribute_definition(
                organization=self.organization,
                name=f"AKC Field {index + 1}",
                field_type=AttributeDefinition.FieldType.TEXT,
                description="AKC MCP configuration regression field.",
            )
        self.assertEqual(
            AttributeDefinition.objects.filter(
                organization=self.organization,
                is_active=True,
            ).count(),
            38,
        )
