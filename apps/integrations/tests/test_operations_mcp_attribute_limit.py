"""Regression coverage for Operations MCP custom-attribute capacity."""

from apps.crm.models import AttributeDefinition
from apps.integrations.models import OperationsPolicy
from apps.integrations.operations_policy import (
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
)
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


class TestOperationsMCPAttributeLimit(OperationsMCPBase):
    def test_reserved_booked_at_does_not_consume_custom_attribute_capacity(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_ATTRIBUTE_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        for index in range(14):
            AttributeDefinition.objects.create(
                organization=self.organization,
                name=f"Capacity Field {index + 1}",
                key=f"capacity_field_{index + 1}",
                field_type=AttributeDefinition.FieldType.TEXT,
                description="Capacity regression fixture.",
            )

        self.assertTrue(
            AttributeDefinition.objects.filter(
                organization=self.organization,
                is_active=True,
                key="booked_at",
            ).exists()
        )
        self.assertEqual(
            AttributeDefinition.objects.filter(
                organization=self.organization,
                is_active=True,
            ).exclude(key="booked_at").count(),
            14,
        )

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Create the fifteenth supported custom attribute.",
                    "data": {
                        "name": "Timeline",
                        "field_type": AttributeDefinition.FieldType.TEXT,
                        "description": "Lead timeline supplied by the prospect.",
                    },
                },
            )
        )

        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["status"], "FIXED")
        self.assertEqual(
            result["structuredContent"]["attribute"]["name"],
            "Timeline",
        )
        self.assertEqual(
            AttributeDefinition.objects.filter(
                organization=self.organization,
                is_active=True,
            ).exclude(key="booked_at").count(),
            15,
        )
