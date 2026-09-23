# ruff: noqa: F403,F405
"""Domain-focused coverage split from the historical Operations MCP suite."""

from apps.integrations.tests.operations_mcp_test_base import *


class TestOperationsMCPAICRM(OperationsMCPBase):
    def test_read_context_does_not_create_missing_policy_row(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        self.assertFalse(
            OperationsPolicy.objects.filter(
                organization=self.organization,
            ).exists()
        )
        result = self._result(
            self._call(bearer, "list_organizations")
        )
        self.assertFalse(result["isError"])
        self.assertFalse(
            OperationsPolicy.objects.filter(
                organization=self.organization,
            ).exists()
        )

    def test_operations_ai_configuration_obeys_canonical_playbook_validator(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        with patch(
            "apps.integrations.operations_tools.validate_playbook",
            side_effect=ValueError(
                "Canonical Playbook validation rejected this configuration."
            ),
        ):
            rejected = self._result(
                self._call(
                    bearer,
                    "update_ai_configuration",
                    {
                        "changes": {
                            "ai_playbook": "## Qualification Questions\nQ1. Invalid fixture",
                        },
                        "reason": "Validate proposed Playbook",
                        "dry_run": True,
                    },
                )
            )

        self.assertTrue(rejected["isError"])
        self.assertEqual(
            rejected["structuredContent"]["status"],
            "FAILED",
        )
        self.assertIn(
            "Canonical Playbook validation rejected",
            rejected["structuredContent"]["error"],
        )
        self.assertFalse(
            OrgInfo.objects.filter(
                organization=self.organization
            ).exists()
        )

    def test_operations_ai_configuration_rejects_secret_like_content(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        blocked = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "ai_playbook": (
                            "Qualification rules. password: super-secret-value"
                        ),
                    },
                    "reason": "Attempt unsafe AI configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )

        allowed = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "ai_playbook": (
                            "Ask only the configured qualification questions "
                            "and never request credentials."
                        ),
                    },
                    "reason": "Review safe AI configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(allowed["isError"])
        self.assertEqual(
            allowed["structuredContent"]["status"],
            "DRY_RUN",
        )

    def test_lead_attribute_write_validates_definition_type_and_options(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
            approval_required_capabilities=[],
        )
        numeric = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Employee Count",
            key="employee_count",
            field_type=AttributeDefinition.FieldType.NUMERIC,
        )
        option = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Priority Band",
            key="priority_band",
            field_type=AttributeDefinition.FieldType.OPTION,
            options=["High", "Low"],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        invalid_numeric = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    "lead_id": str(self.lead.id),
                    "values": {
                        numeric.key: "not-a-number",
                    },
                    "reason": "Validate numeric CRM attribute value",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(invalid_numeric["isError"])
        self.assertEqual(
            invalid_numeric["structuredContent"]["status"],
            "FAILED",
        )

        invalid_option = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    "lead_id": str(self.lead.id),
                    "values": {
                        option.key: "Medium",
                    },
                    "reason": "Validate option CRM attribute value",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(invalid_option["isError"])
        self.assertEqual(
            invalid_option["structuredContent"]["status"],
            "FAILED",
        )

        valid = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    "lead_id": str(self.lead.id),
                    "values": {
                        numeric.key: "25",
                        option.key: "High",
                    },
                    "reason": "Review valid CRM attribute values",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(valid["isError"])
        self.assertEqual(
            valid["structuredContent"]["status"],
            "DRY_RUN",
        )

    def test_approved_lead_attribute_write_rejects_definition_schema_drift(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
            approval_required_capabilities=[
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
        )
        definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Qualification Note",
            key="qualification_note_drift",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "Initial",
        }
        self.lead.save(
            update_fields=["attributes", "updated_at"]
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "values": {
                definition.key: "Reviewed",
            },
            "reason": "Update reviewed qualification note",
        }
        dry = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    **arguments,
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(dry["isError"])

        definition.field_type = (
            AttributeDefinition.FieldType.OPTION
        )
        definition.options = ["Allowed"]
        definition.full_clean()
        definition.save(
            update_fields=[
                "field_type",
                "options",
                "updated_at",
            ]
        )

        stale = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry[
                        "structuredContent"
                    ]["approval_event_id"],
                },
            )
        )
        self.assertTrue(stale["isError"])
        self.assertEqual(
            stale["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.attributes[definition.key],
            "Initial",
        )

    def test_approved_lead_attribute_write_rejects_human_edit_after_dry_run(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
            approval_required_capabilities=[
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
        )
        definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Company Size",
            key="company_size_lock_test",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "10",
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "values": {definition.key: "25"},
            "reason": "Update reviewed company size",
        }
        dry = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        self.lead.refresh_from_db()
        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "18",
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

        stale = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(stale["isError"])
        self.assertEqual(
            stale["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.attributes[definition.key],
            "18",
        )

    def test_approved_ai_configuration_rejects_human_edit_after_dry_run(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[
                CAP_AI_CONFIG_WRITE,
            ],
        )
        info = OrgInfo.objects.create(
            organization=self.organization,
            about="Initial business profile",
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "changes": {
                "about": "AI proposed business profile",
            },
            "reason": "Update reviewed business profile",
        }
        dry = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        info.about = "Human edited business profile"
        info.save(update_fields=["about", "updated_at"])

        stale = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(stale["isError"])
        self.assertEqual(
            stale["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        info.refresh_from_db()
        self.assertEqual(
            info.about,
            "Human edited business profile",
        )

    def test_successful_mutation_rolls_back_if_operations_audit_cannot_persist(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        self.assertFalse(
            OrgInfo.objects.filter(
                organization=self.organization
            ).exists()
        )

        with patch(
            "apps.integrations.views.operations_mcp._record_audit",
            side_effect=RuntimeError("audit storage unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self._call(
                    bearer,
                    "update_ai_configuration",
                    {
                        "changes": {
                            "about": "This must not commit without audit",
                        },
                        "reason": "Test atomic audit requirement",
                        "dry_run": False,
                    },
                )

        self.assertFalse(
            OrgInfo.objects.filter(
                organization=self.organization
            ).exists(),
            "Customer state must roll back if the required audit row cannot persist.",
        )

    def test_operations_ai_configuration_can_create_first_org_info_and_verify(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )

        applied = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "about": "Test organization business profile",
                        "ai_playbook": (
                            "Answer business questions from approved context and "
                            "use backend qualification rules."
                        ),
                    },
                    "reason": "Create initial AI configuration",
                    "dry_run": False,
                },
            )
        )
        self.assertFalse(applied["isError"])
        self.assertEqual(
            applied["structuredContent"]["status"],
            "FIXED",
        )
        self.assertEqual(
            applied["structuredContent"]["verification"],
            "passed",
        )

        info = OrgInfo.objects.get(organization=self.organization)
        self.assertEqual(
            info.about,
            "Test organization business profile",
        )
        self.assertIn(
            "backend qualification rules",
            info.ai_playbook,
        )

    def test_ai_configuration_dry_run_does_not_create_org_info(self):
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
                    "reason": "Prepare AI configuration review",
                },
            )
        )
        self.assertFalse(selected["isError"])
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )

        dry = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "about": "Test organization context",
                    },
                    "reason": "Review proposed AI context change",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(dry["isError"])
        self.assertEqual(dry["structuredContent"]["status"], "DRY_RUN")
        self.assertFalse(
            OrgInfo.objects.filter(organization=self.organization).exists()
        )
