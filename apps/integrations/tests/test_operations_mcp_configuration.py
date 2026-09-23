# ruff: noqa: F403,F405
"""Domain-focused coverage split from the historical Operations MCP suite."""

from apps.integrations.tests.operations_mcp_test_base import *


class TestOperationsMCPConfiguration(OperationsMCPBase):
    def test_messaging_automation_settings_reports_account_truncation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        existing_count = WhatsAppAccount.objects.filter(
            organization=self.organization,
            is_active=True,
        ).count()
        needed = max(0, 51 - existing_count)
        WhatsAppAccount.objects.bulk_create(
            [
                WhatsAppAccount(
                    organization=self.organization,
                    connection_type=WhatsAppAccount.ConnectionType.API,
                    business_name=f"Bounded Settings {index:03d}",
                    display_phone_number=f"+9187{index:010d}",
                    status=WhatsAppAccount.Status.CONNECTED,
                    is_active=True,
                )
                for index in range(needed)
            ]
        )
        expected_count = WhatsAppAccount.objects.filter(
            organization=self.organization,
            is_active=True,
        ).count()
        self.assertGreaterEqual(expected_count, 51)
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        result = self._result(
            self._call(
                bearer,
                "get_messaging_automation_settings",
                {},
            )
        )

        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        self.assertEqual(data["count"], 50)
        self.assertEqual(data["accounts_returned"], 50)
        self.assertEqual(data["account_count"], expected_count)
        self.assertTrue(data["accounts_truncated"])

    def test_messaging_automation_settings_dry_run_apply_and_verify(self):
        OperationsPolicy.objects.create(
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
        pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Messaging Operations Pipeline",
            phone_number="+919000000030",
            ai_enabled=True,
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Messaging Operations Sender",
            display_phone_number="+919000000030",
            phone_number_id="messaging-ops-sender",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.organization.refresh_from_db()
        organization_settings = dict(
            self.organization.settings or {}
        )
        hosted_settings = dict(
            organization_settings.get(
                "hosted_whatsapp"
            )
            or {}
        )
        sessions = dict(
            hosted_settings.get("sessions")
            or {}
        )
        account_settings = dict(
            sessions.get(str(account.id))
            or {}
        )
        account_settings[
            "legacy_internal_marker"
        ] = "internal-messaging-setting"
        sessions[str(account.id)] = account_settings
        hosted_settings["sessions"] = sessions
        organization_settings[
            "hosted_whatsapp"
        ] = hosted_settings
        self.organization.settings = organization_settings
        self.organization.save(
            update_fields=["settings", "updated_at"]
        )

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        initial = self._result(
            self._call(
                bearer,
                "get_messaging_automation_settings",
                {
                    "whatsapp_account_id": str(account.id),
                },
            )
        )
        self.assertFalse(initial["isError"])
        self.assertEqual(
            initial["structuredContent"]["accounts"][0][
                "pipeline"
            ]["id"],
            str(pipeline.id),
        )
        initial_settings = (
            initial["structuredContent"]["accounts"][0][
                "settings"
            ]
        )
        self.assertTrue(
            initial_settings["ai_auto_reply"]
        )
        self.assertNotIn(
            "legacy_internal_marker",
            initial_settings,
        )
        self.assertNotIn(
            "internal-messaging-setting",
            json.dumps(initial),
        )

        arguments = {
            "whatsapp_account_id": str(account.id),
            "changes": {
                "ai_auto_reply": False,
                "auto_follow_up": False,
                "business_hours_start": "09:15",
                "business_hours_end": "18:30",
                "active_conversation_delay_value": 45,
                "active_conversation_delay_unit": "minutes",
            },
            "reason": "Configure reviewed pipeline messaging automation",
        }
        dry = self._result(
            self._call(
                bearer,
                "update_messaging_automation_settings",
                {
                    **arguments,
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(dry["isError"])
        self.assertEqual(
            dry["structuredContent"]["status"],
            "DRY_RUN",
        )
        self.assertTrue(
            dry["structuredContent"]["approval_required"]
        )
        self.assertIn(
            "approval_event_id",
            dry["structuredContent"],
        )
        self.assertNotIn(
            "legacy_internal_marker",
            dry["structuredContent"]["before"],
        )
        self.assertNotIn(
            "legacy_internal_marker",
            dry["structuredContent"]["after"],
        )

        pipeline.refresh_from_db()
        self.assertTrue(pipeline.ai_enabled)
        persisted_before = get_session_settings(
            account=account
        )
        self.assertTrue(persisted_before["auto_follow_up"])
        self.assertNotEqual(
            persisted_before["business_hours_start"],
            "09:15",
        )

        applied = self._result(
            self._call(
                bearer,
                "update_messaging_automation_settings",
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
        self.assertFalse(applied["isError"])
        self.assertEqual(
            applied["structuredContent"]["status"],
            "FIXED",
        )
        self.assertEqual(
            applied["structuredContent"]["verification"],
            "passed",
        )
        self.assertNotIn(
            "legacy_internal_marker",
            applied["structuredContent"]["settings"],
        )

        pipeline.refresh_from_db()
        self.assertFalse(pipeline.ai_enabled)
        persisted = get_session_settings(account=account)
        self.assertFalse(persisted["ai_auto_reply"])
        self.assertFalse(persisted["auto_follow_up"])
        self.assertEqual(
            persisted["business_hours_start"],
            "09:15",
        )
        self.assertEqual(
            persisted["business_hours_end"],
            "18:30",
        )
        self.assertEqual(
            persisted["active_conversation_delay_value"],
            45,
        )
        self.assertEqual(
            persisted["active_conversation_delay_unit"],
            "minutes",
        )

    def test_messaging_automation_approval_is_invalid_after_human_change(self):
        OperationsPolicy.objects.create(
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
        pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Messaging Stale Pipeline",
            phone_number="+919000000031",
            ai_enabled=True,
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Messaging Stale Sender",
            display_phone_number="+919000000031",
            phone_number_id="messaging-stale-sender",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "whatsapp_account_id": str(account.id),
            "changes": {
                "business_hours_start": "08:30",
            },
            "reason": "Update reviewed messaging business hours",
        }
        dry = self._result(
            self._call(
                bearer,
                "update_messaging_automation_settings",
                {
                    **arguments,
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(dry["isError"])

        update_session_settings(
            account=account,
            payload={
                "business_hours_start": "07:45",
            },
        )

        stale = self._result(
            self._call(
                bearer,
                "update_messaging_automation_settings",
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
        persisted = get_session_settings(account=account)
        self.assertEqual(
            persisted["business_hours_start"],
            "07:45",
        )
        pipeline.refresh_from_db()
        self.assertTrue(pipeline.ai_enabled)

    def test_organization_configuration_reports_explicit_truncation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        Pipeline.objects.bulk_create(
            [
                Pipeline(
                    organization=self.organization,
                    name=f"Bounded Pipeline {index:03d}",
                )
                for index in range(101)
            ]
        )
        AttributeDefinition.objects.bulk_create(
            [
                AttributeDefinition(
                    organization=self.organization,
                    name=f"Bounded Attribute {index:03d}",
                    key=f"bounded_attribute_{index:03d}",
                    field_type=AttributeDefinition.FieldType.TEXT,
                    display_order=index,
                )
                for index in range(101)
            ]
        )
        bounded_stage_pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Bounded Pipeline 000",
        )
        Stage.objects.bulk_create(
            [
                Stage(
                    pipeline=bounded_stage_pipeline,
                    name=f"Bounded Stage {index:03d}",
                    display_order=index,
                )
                for index in range(101)
            ]
        )
        option_attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Bounded Option Attribute",
            key="bounded_option_attribute",
            field_type=AttributeDefinition.FieldType.OPTION,
            options=[
                f"Option {index:03d}"
                for index in range(101)
            ],
            display_order=0,
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
                "get_organization_configuration",
                {},
            )
        )
        self.assertFalse(result["isError"])
        config = result["structuredContent"]
        counts = config["counts"]
        self.assertGreater(
            counts["pipeline_count"],
            counts["pipelines_returned"],
        )
        self.assertEqual(
            counts["pipelines_returned"],
            100,
        )
        self.assertTrue(
            counts["pipelines_truncated"]
        )
        self.assertGreater(
            counts["attribute_count"],
            counts["attributes_returned"],
        )
        self.assertEqual(
            counts["attributes_returned"],
            100,
        )
        self.assertTrue(
            counts["attributes_truncated"]
        )
        bounded_pipeline_row = next(
            item
            for item in config["pipelines"]
            if item["id"] == str(
                bounded_stage_pipeline.id
            )
        )
        self.assertEqual(
            bounded_pipeline_row["stage_count"],
            101,
        )
        self.assertEqual(
            bounded_pipeline_row["stages_returned"],
            100,
        )
        self.assertEqual(
            len(bounded_pipeline_row["stages"]),
            100,
        )
        self.assertTrue(
            bounded_pipeline_row["stages_truncated"]
        )
        option_row = next(
            item
            for item in config["attributes"]
            if item["id"] == str(option_attribute.id)
        )
        self.assertEqual(
            option_row["option_count"],
            101,
        )
        self.assertEqual(
            len(option_row["options"]),
            100,
        )
        self.assertTrue(
            option_row["options_truncated"]
        )
        for pipeline in config["pipelines"]:
            self.assertIn("stage_count", pipeline)
            self.assertIn(
                "stages_returned",
                pipeline,
            )
            self.assertIn(
                "stages_truncated",
                pipeline,
            )

    def test_operations_reads_report_bounded_automation_truncation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Truncation Sender",
            display_phone_number="+919000000040",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        for index in range(2):
            SmartTrigger.objects.create(
                organization=self.organization,
                name=f"Bounded Workflow {index}",
                enabled=True,
                position=index + 1,
                trigger_type="keyword",
                conditions={
                    "scopes": [],
                    "attributes": [],
                    "keywords": [f"word-{index}"],
                },
                action_type="ai",
                action={"enabled": True},
                fingerprint=(str(index + 4) * 64),
                created_by=self.admin,
            )
            FollowupSequence.objects.create(
                organization=self.organization,
                created_by=self.admin,
                name=f"Bounded Cadence {index}",
                description="Bounded automation list test",
                whatsapp_account=account,
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
                "get_automation_configuration",
                {"limit": 1},
            )
        )
        self.assertFalse(result["isError"])
        counts = result["structuredContent"]["counts"]
        self.assertEqual(counts["workflow_count"], 2)
        self.assertEqual(counts["cadence_count"], 2)
        self.assertEqual(counts["workflows_returned"], 1)
        self.assertEqual(counts["cadences_returned"], 1)
        self.assertTrue(counts["workflows_truncated"])
        self.assertTrue(counts["cadences_truncated"])

    def test_automation_read_fails_closed_on_foreign_workflow_references(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        foreign_account = WhatsAppAccount.objects.create(
            organization=self.other_organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Foreign Workflow Sender",
            display_phone_number="+919000000041",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        workflow = SmartTrigger.objects.create(
            organization=self.organization,
            name="Corrupt Cross Tenant Workflow",
            enabled=False,
            position=1,
            trigger_type="keyword",
            conditions={
                "scopes": [
                    {
                        "pipeline": str(
                            self.other_lead.pipeline_id
                        ),
                        "stages": [
                            str(self.other_lead.stage_id)
                        ],
                    }
                ],
                "attributes": [],
                "keywords": ["hello"],
            },
            action_type="ai",
            action={"enabled": True},
            fingerprint="8" * 64,
            created_by=self.admin,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        foreign_scope = self._result(
            self._call(
                bearer,
                "get_automation_configuration",
                {},
            )
        )
        self.assertTrue(foreign_scope["isError"])
        self.assertEqual(
            foreign_scope["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        foreign_scope_payload = json.dumps(
            foreign_scope
        )
        self.assertNotIn(
            str(self.other_lead.pipeline_id),
            foreign_scope_payload,
        )
        self.assertNotIn(
            str(self.other_lead.stage_id),
            foreign_scope_payload,
        )
        self.assertNotIn(
            self.other_organization.name,
            foreign_scope_payload,
        )

        workflow.conditions = {
            "scopes": [
                {
                    "pipeline": str(self.pipeline.id),
                    "stages": [str(self.new_stage.id)],
                }
            ],
            "attributes": [],
            "keywords": ["hello"],
        }
        workflow.action_type = "message"
        workflow.action = {
            "body": "Hello from a corrupt stored rule",
            "account": str(foreign_account.id),
            "schedule": "relative",
            "duration": 1,
            "unit": "hours",
        }
        workflow.save(
            update_fields=[
                "conditions",
                "action_type",
                "action",
                "updated_at",
            ]
        )

        foreign_action = self._result(
            self._call(
                bearer,
                "get_automation_configuration",
                {},
            )
        )
        self.assertTrue(foreign_action["isError"])
        self.assertEqual(
            foreign_action["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        foreign_action_payload = json.dumps(
            foreign_action
        )
        self.assertNotIn(
            str(foreign_account.id),
            foreign_action_payload,
        )
        self.assertNotIn(
            self.other_organization.name,
            foreign_action_payload,
        )

    def test_automation_read_fails_closed_on_stale_workflow_references(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        legacy_attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Legacy Sensitive Context",
            key="legacy_sensitive_context",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        workflow = SmartTrigger.objects.create(
            organization=self.organization,
            name="Stale Workflow Reference",
            enabled=False,
            position=1,
            trigger_type="keyword",
            conditions={
                "scopes": [
                    {
                        "pipeline": str(self.pipeline.id),
                        "stages": [str(self.new_stage.id)],
                    }
                ],
                "attributes": [
                    {
                        "key": legacy_attribute.key,
                        "match": "equals",
                        "values": ["short-private-value"],
                    }
                ],
                "keywords": ["hello"],
            },
            action_type="ai",
            action={"enabled": True},
            fingerprint="9" * 64,
            created_by=self.admin,
        )
        legacy_key = legacy_attribute.key
        legacy_attribute.delete()

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        orphan_attribute = self._result(
            self._call(
                bearer,
                "get_automation_configuration",
                {},
            )
        )
        self.assertTrue(orphan_attribute["isError"])
        self.assertEqual(
            orphan_attribute["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        orphan_payload = json.dumps(orphan_attribute)
        self.assertNotIn(legacy_key, orphan_payload)
        self.assertNotIn(
            "short-private-value",
            orphan_payload,
        )

        workflow.conditions = {
            "scopes": [
                {
                    "pipeline": str(self.pipeline.id),
                    "stages": [str(self.review_stage.id)],
                }
            ],
            "attributes": [],
            "keywords": ["hello"],
        }
        workflow.save(
            update_fields=[
                "conditions",
                "updated_at",
            ]
        )
        stale_stage_id = str(self.review_stage.id)
        self.review_stage.is_active = False
        self.review_stage.save(
            update_fields=["is_active", "updated_at"]
        )

        stale_stage = self._result(
            self._call(
                bearer,
                "get_automation_configuration",
                {},
            )
        )
        self.assertTrue(stale_stage["isError"])
        self.assertEqual(
            stale_stage["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertNotIn(
            stale_stage_id,
            json.dumps(stale_stage),
        )

    def test_automation_read_reports_cadence_step_truncation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Bounded Cadence Sender",
            display_phone_number="+919000000006",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Bounded Step Cadence",
            description="Many steps",
            whatsapp_account=account,
        )
        FollowupStep.objects.bulk_create(
            [
                FollowupStep(
                    sequence=sequence,
                    position=index + 1,
                    step_type=FollowupStep.StepType.REMINDER,
                    title=f"Reminder {index:03d}",
                    reminder_text=f"Reminder body {index:03d}",
                    schedule_type=(
                        FollowupStep.ScheduleType.IMMEDIATE
                    ),
                )
                for index in range(101)
            ]
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
                "get_automation_configuration",
                {},
            )
        )
        self.assertFalse(result["isError"])
        cadence = next(
            item
            for item in result["structuredContent"][
                "cadences"
            ]
            if item["id"] == str(sequence.id)
        )
        self.assertEqual(cadence["step_count"], 101)
        self.assertEqual(cadence["steps_returned"], 100)
        self.assertEqual(len(cadence["steps"]), 100)
        self.assertTrue(cadence["steps_truncated"])

    def test_automation_read_checks_truncated_cadence_steps_for_foreign_templates(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Bounded Cadence Safety Sender",
            display_phone_number="+919000000007",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Bounded Cadence Safety",
            description="Foreign template after visible window",
            whatsapp_account=account,
        )
        FollowupStep.objects.bulk_create(
            [
                FollowupStep(
                    sequence=sequence,
                    position=index + 1,
                    step_type=FollowupStep.StepType.REMINDER,
                    title=f"Safe reminder {index:03d}",
                    reminder_text=f"Safe body {index:03d}",
                    schedule_type=(
                        FollowupStep.ScheduleType.IMMEDIATE
                    ),
                )
                for index in range(100)
            ]
        )
        foreign_account = WhatsAppAccount.objects.create(
            organization=self.other_organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Foreign Cadence Sender",
            display_phone_number="+919000000008",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        foreign_template = WhatsAppTemplate.objects.create(
            organization=self.other_organization,
            account=foreign_account,
            name="Foreign Hidden Template",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Foreign hidden body",
            created_by=self.other_admin,
        )
        FollowupStep.objects.create(
            sequence=sequence,
            position=101,
            step_type=FollowupStep.StepType.WHATSAPP,
            title="Hidden foreign step",
            whatsapp_template=foreign_template,
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
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
                "get_automation_configuration",
                {},
            )
        )

        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        payload = json.dumps(result)
        self.assertNotIn(str(foreign_template.id), payload)
        self.assertNotIn(str(foreign_account.id), payload)
        self.assertNotIn(foreign_template.name, payload)

    def test_automation_read_fails_closed_on_cadence_template_sender_mismatch(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        cadence_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Cadence Primary Sender",
            display_phone_number="+919000000004",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        other_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Cadence Other Sender",
            display_phone_number="+919000000005",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Corrupt Template Sender Cadence",
            description="Stored mismatch fixture",
            whatsapp_account=cadence_account,
        )
        template = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=other_account,
            name="Wrong Sender Template",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Hello from the wrong sender",
            created_by=self.admin,
        )
        FollowupStep.objects.create(
            sequence=sequence,
            position=1,
            step_type=FollowupStep.StepType.WHATSAPP,
            title=template.name,
            whatsapp_template=template,
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
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
                "get_automation_configuration",
                {},
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        payload = json.dumps(result)
        self.assertNotIn(str(template.id), payload)
        self.assertNotIn(str(other_account.id), payload)
        self.assertNotIn(template.name, payload)

    def test_existing_cadence_sender_change_is_explicitly_rejected(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        first = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="First Sender",
            display_phone_number="+919000000001",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        second = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Second Sender",
            display_phone_number="+919000000002",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Existing Cadence",
            description="Test",
            whatsapp_account=first,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        result = self._result(
            self._call(
                bearer,
                "upsert_cadence_configuration",
                {
                    "cadence_id": str(sequence.id),
                    "data": {
                        "name": sequence.name,
                        "description": sequence.description,
                        "provider": "api",
                        "whatsapp_account_id": str(second.id),
                    },
                    "reason": "Attempt sender change on cadence",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        sequence.refresh_from_db()
        self.assertEqual(sequence.whatsapp_account_id, first.id)

    def test_operations_automation_writes_reject_secret_like_content(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Automation Sender",
            display_phone_number="+919000000003",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Safe Cadence",
            description="Test",
            whatsapp_account=account,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        workflow = self._result(
            self._call(
                bearer,
                "upsert_workflow_configuration",
                {
                    "data": {
                        "name": "password: workflow-secret",
                    },
                    "reason": "Attempt unsafe workflow configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(workflow["isError"])
        self.assertEqual(
            workflow["structuredContent"]["status"],
            "NOT_ALLOWED",
        )

        cadence_step = self._result(
            self._call(
                bearer,
                "add_cadence_step",
                {
                    "cadence_id": str(sequence.id),
                    "data": {
                        "type": "reminder",
                        "text": "password: cadence-secret",
                        "schedule": {"type": "immediate"},
                    },
                    "reason": "Attempt unsafe cadence content",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(cadence_step["isError"])
        self.assertEqual(
            cadence_step["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertEqual(sequence.steps.count(), 0)

    def test_operations_crm_and_lead_writes_reject_secret_like_values(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
            approval_required_capabilities=[],
        )
        context_attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Customer Context",
            key="customer_context",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        pipeline_result = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {
                    "data": {
                        "name": "Unsafe Pipeline",
                        "description": "api_key: pipeline-secret",
                    },
                    "reason": "Attempt unsafe pipeline configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(pipeline_result["isError"])
        self.assertEqual(
            pipeline_result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertFalse(
            Pipeline.objects.filter(
                organization=self.organization,
                name="Unsafe Pipeline",
            ).exists()
        )

        lead_result = self._result(
            self._call(
                bearer,
                "update_lead_attributes",
                {
                    "lead_id": str(self.lead.id),
                    "values": {
                        context_attribute.key: "password: lead-secret",
                    },
                    "reason": "Attempt unsafe lead attribute write",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(lead_result["isError"])
        self.assertEqual(
            lead_result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.lead.refresh_from_db()
        self.assertNotIn(
            context_attribute.key,
            self.lead.attributes or {},
        )

    def test_pipeline_configuration_cannot_deactivate_pipeline_with_leads(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        result = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {
                    "pipeline_id": str(self.pipeline.id),
                    "data": {"is_active": False},
                    "reason": "Attempt pipeline deactivation with live leads",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.pipeline.refresh_from_db()
        self.assertTrue(self.pipeline.is_active)

    def test_attribute_type_change_rejects_incompatible_existing_lead_values(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Legacy Context",
            key="legacy_context",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "not-a-number",
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

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
                    "attribute_id": str(definition.id),
                    "data": {
                        "name": definition.name,
                        "field_type": "numeric",
                        "description": "",
                        "options": [],
                    },
                    "reason": "Attempt incompatible attribute type change",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "existing CRM values invalid",
            result["structuredContent"]["error"],
        )
        definition.refresh_from_db()
        self.assertEqual(
            definition.field_type,
            AttributeDefinition.FieldType.TEXT,
        )

        self.lead.attributes[definition.key] = "42"
        self.lead.save(update_fields=["attributes", "updated_at"])
        allowed = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {
                    "attribute_id": str(definition.id),
                    "data": {
                        "name": definition.name,
                        "field_type": "numeric",
                        "description": "",
                        "options": [],
                    },
                    "reason": "Review compatible attribute type change",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(allowed["isError"])
        self.assertEqual(
            allowed["structuredContent"]["status"],
            "DRY_RUN",
        )

    def test_attribute_type_change_bounds_existing_value_scan(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Bounded Context",
            key="bounded_context",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "42",
        }
        self.lead.save(
            update_fields=["attributes", "updated_at"]
        )
        Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Second Bounded Context Lead",
            phone="+918600000099",
            attributes={definition.key: "84"},
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        with patch(
            "apps.integrations.operations_tools."
            "ATTRIBUTE_COMPATIBILITY_SCAN_LIMIT",
            1,
        ):
            result = self._result(
                self._call(
                    bearer,
                    "upsert_attribute_configuration",
                    {
                        "attribute_id": str(definition.id),
                        "data": {
                            "name": definition.name,
                            "field_type": "numeric",
                            "description": "",
                            "options": [],
                        },
                        "reason": (
                            "Review bounded attribute type migration"
                        ),
                        "dry_run": True,
                    },
                )
            )

        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "MANUAL_FIX_REQUIRED",
        )
        self.assertIn(
            "unbounded compatibility scan",
            result["structuredContent"]["error"],
        )
        definition.refresh_from_db()
        self.assertEqual(
            definition.field_type,
            AttributeDefinition.FieldType.TEXT,
        )

    def test_cadence_step_verification_checks_schedule_and_content(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Cadence Verify Sender",
            display_phone_number="+919000000004",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Verification Cadence",
            description="Test",
            whatsapp_account=account,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "cadence_id": str(sequence.id),
            "data": {
                "type": "reminder",
                "text": "Call this lead",
                "schedule": {
                    "type": "delay",
                    "delay_value": 2,
                    "delay_unit": "hours",
                },
            },
            "reason": "Add verified reminder cadence step",
            "dry_run": False,
        }

        applied = self._result(
            self._call(
                bearer,
                "add_cadence_step",
                arguments,
            )
        )
        self.assertFalse(applied["isError"])
        self.assertEqual(
            applied["structuredContent"]["status"],
            "FIXED",
        )
        step = sequence.steps.get()
        self.assertEqual(step.step_type, FollowupStep.StepType.REMINDER)
        self.assertEqual(step.reminder_text, "Call this lead")
        self.assertEqual(step.schedule_type, FollowupStep.ScheduleType.DELAY)
        self.assertEqual(step.delay_value, 2)
        self.assertEqual(step.delay_unit, FollowupStep.DelayUnit.HOURS)

        sequence.steps.all().delete()

        def create_wrong_schedule(*, sequence, text, **kwargs):
            return FollowupStep.objects.create(
                sequence=sequence,
                position=sequence.steps.count() + 1,
                step_type=FollowupStep.StepType.REMINDER,
                title="Wrong schedule",
                reminder_text=text,
                schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
            )

        with patch(
            "apps.integrations.operations_tools.add_reminder_step",
            side_effect=create_wrong_schedule,
        ):
            failed = self._result(
                self._call(
                    bearer,
                    "add_cadence_step",
                    {
                        **arguments,
                        "reason": "Detect mismatched cadence persistence",
                    },
                )
            )
        self.assertTrue(failed["isError"])
        self.assertEqual(
            failed["structuredContent"]["status"],
            "FAILED",
        )
        self.assertIn(
            "schedule_type",
            failed["structuredContent"]["error"],
        )
        self.assertEqual(
            sequence.steps.count(),
            0,
            "Verification failure must roll back the malformed Cadence step.",
        )

    def test_stage_config_approval_is_invalid_after_human_edit(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[CAP_CRM_CONFIG_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "pipeline_id": str(self.pipeline.id),
            "stage_id": str(self.review_stage.id),
            "data": {
                "name": self.review_stage.name,
                "description": "Approved stage description",
                "display_order": self.review_stage.display_order,
                "is_active": True,
                "ai_on": True,
            },
            "reason": "Update reviewed stage description",
        }
        dry = self._result(
            self._call(
                bearer,
                "upsert_stage_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        self.review_stage.description = "Human changed stage description"
        self.review_stage.save(
            update_fields=["description", "updated_at"]
        )

        stale = self._result(
            self._call(
                bearer,
                "upsert_stage_configuration",
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
        self.review_stage.refresh_from_db()
        self.assertEqual(
            self.review_stage.description,
            "Human changed stage description",
        )

    def test_workflow_config_approval_is_invalid_after_human_edit(self):
        OperationsPolicy.objects.create(
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
        workflow = SmartTrigger.objects.create(
            organization=self.organization,
            name="Existing Workflow",
            enabled=False,
            position=1,
            trigger_type="keyword",
            conditions={
                "scopes": [
                    {
                        "pipeline": str(self.pipeline.id),
                        "stages": [str(self.new_stage.id)],
                    }
                ],
                "attributes": [],
                "keywords": ["hello"],
            },
            action_type="ai",
            action={"enabled": True},
            fingerprint="3" * 64,
            created_by=self.admin,
        )
        data = {
            "name": "Existing Workflow",
            "enabled": False,
            "trigger_type": "keyword",
            "conditions": {
                "scopes": [
                    {
                        "pipeline": str(self.pipeline.id),
                        "stages": [str(self.new_stage.id)],
                    }
                ],
                "attributes": [],
                "keywords": ["hello"],
            },
            "action_type": "ai",
            "action": {"enabled": True},
        }
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "workflow_id": str(workflow.id),
            "data": data,
            "reason": "Update reviewed workflow configuration",
        }
        dry = self._result(
            self._call(
                bearer,
                "upsert_workflow_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        workflow.enabled = True
        workflow.name = "Human Edited Workflow"
        workflow.save(
            update_fields=["enabled", "name", "updated_at"]
        )

        stale = self._result(
            self._call(
                bearer,
                "upsert_workflow_configuration",
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
        workflow.refresh_from_db()
        self.assertTrue(workflow.enabled)
        self.assertEqual(workflow.name, "Human Edited Workflow")

    def test_operations_cadence_paths_never_decrypt_provider_credentials(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CADENCE_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        api_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Credential Safe API Sender",
            display_phone_number="+919000000018",
            phone_number_id="credential-safe-api",
            access_token="api-cadence-provider-secret",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Credential Safe Hosted Sender",
            display_phone_number="+919000000019",
            phone_number_id="+919000000019",
            access_token="hosted-cadence-provider-secret",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Credential Safe Existing Cadence",
            description="Initial description",
            whatsapp_account=api_account,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[
                OPERATIONS_READ_SCOPE,
                OPERATIONS_WRITE_SCOPE,
            ],
        )

        with patch(
            "apps.channels.models.EncryptedTextField.from_db_value",
            side_effect=AssertionError(
                "Operations Cadence paths must not decrypt provider credentials"
            ),
        ):
            inspected = self._result(
                self._call(
                    bearer,
                    "get_automation_configuration",
                    {},
                )
            )
            self.assertFalse(inspected["isError"])

            updated = self._result(
                self._call(
                    bearer,
                    "upsert_cadence_configuration",
                    {
                        "cadence_id": str(sequence.id),
                        "data": {
                            "name": sequence.name,
                            "description": "Reviewed safe description",
                            "provider": "api",
                            "whatsapp_account_id": str(api_account.id),
                        },
                        "reason": "Update reviewed Cadence description",
                        "dry_run": False,
                    },
                )
            )
            self.assertFalse(updated["isError"])
            self.assertEqual(
                updated["structuredContent"]["status"],
                "FIXED",
            )

            hosted_created = self._result(
                self._call(
                    bearer,
                    "upsert_cadence_configuration",
                    {
                        "data": {
                            "name": "Credential Safe Hosted Cadence",
                            "description": "Hosted cadence without credential reads",
                            "provider": "hosted",
                        },
                        "reason": "Create reviewed Hosted Cadence",
                        "dry_run": False,
                    },
                )
            )
            self.assertFalse(hosted_created["isError"])
            self.assertEqual(
                hosted_created["structuredContent"]["status"],
                "FIXED",
            )

            step_preview = self._result(
                self._call(
                    bearer,
                    "add_cadence_step",
                    {
                        "cadence_id": str(sequence.id),
                        "data": {
                            "type": "reminder",
                            "text": "Review this lead",
                            "schedule": {"type": "immediate"},
                        },
                        "reason": "Add reviewed Cadence reminder",
                        "dry_run": True,
                    },
                )
            )
            self.assertFalse(step_preview["isError"])
            self.assertEqual(
                step_preview["structuredContent"]["status"],
                "DRY_RUN",
            )

        sequence.refresh_from_db()
        self.assertEqual(
            sequence.description,
            "Reviewed safe description",
        )
        self.assertTrue(
            FollowupSequence.objects.filter(
                organization=self.organization,
                name="Credential Safe Hosted Cadence",
            ).exists()
        )

    def test_cadence_config_approval_is_invalid_after_human_edit(self):
        OperationsPolicy.objects.create(
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
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Stale Cadence Sender",
            display_phone_number="+919000000020",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Stale Cadence",
            description="Initial description",
            whatsapp_account=account,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "cadence_id": str(sequence.id),
            "data": {
                "name": sequence.name,
                "description": "Approved description",
                "provider": "api",
                "whatsapp_account_id": str(account.id),
            },
            "reason": "Update reviewed Cadence description",
        }
        dry = self._result(
            self._call(
                bearer,
                "upsert_cadence_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        sequence.description = "Human edited Cadence description"
        sequence.save(
            update_fields=["description", "updated_at"]
        )

        stale = self._result(
            self._call(
                bearer,
                "upsert_cadence_configuration",
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
        sequence.refresh_from_db()
        self.assertEqual(
            sequence.description,
            "Human edited Cadence description",
        )

    def test_cadence_step_approval_is_invalid_after_step_list_changes(self):
        OperationsPolicy.objects.create(
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
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Stale Step Sender",
            display_phone_number="+919000000021",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            created_by=self.admin,
            name="Stale Step Cadence",
            description="",
            whatsapp_account=account,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "cadence_id": str(sequence.id),
            "data": {
                "type": "reminder",
                "text": "Approved next reminder",
                "schedule": {"type": "immediate"},
            },
            "reason": "Add reviewed Cadence reminder",
        }
        dry = self._result(
            self._call(
                bearer,
                "add_cadence_step",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        manual_step = FollowupStep.objects.create(
            sequence=sequence,
            position=1,
            step_type=FollowupStep.StepType.REMINDER,
            title="Human step",
            reminder_text="Human-added reminder",
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )

        stale = self._result(
            self._call(
                bearer,
                "add_cadence_step",
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
        self.assertEqual(sequence.steps.count(), 1)
        self.assertTrue(
            sequence.steps.filter(pk=manual_step.pk).exists()
        )

    def test_operations_configuration_rejects_bare_opaque_secret_values(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AI_CONFIG_WRITE,
                CAP_AUTOMATION_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        opaque = "A" * 64

        ai_result = self._result(
            self._call(
                bearer,
                "update_ai_configuration",
                {
                    "changes": {
                        "ai_playbook": (
                            "Use normal qualification guidance. "
                            + opaque
                        ),
                    },
                    "reason": "Review proposed AI configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(ai_result["isError"])
        self.assertEqual(
            ai_result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )

        workflow_result = self._result(
            self._call(
                bearer,
                "upsert_workflow_configuration",
                {
                    "data": {
                        "name": "Unsafe opaque value " + opaque,
                    },
                    "reason": "Review proposed workflow configuration",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(workflow_result["isError"])
        self.assertEqual(
            workflow_result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        payload = json.dumps(
            {
                "ai": ai_result,
                "workflow": workflow_result,
            }
        )
        self.assertNotIn(opaque, payload)

    def test_pipeline_configuration_create_verifies_standard_stages(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {
                    "data": {
                        "name": "Enterprise Sales",
                        "description": "Enterprise pipeline",
                        "is_active": True,
                        "ai_enabled": True,
                    },
                    "reason": "Create enterprise sales pipeline",
                    "dry_run": False,
                },
            )
        )
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "FIXED",
        )
        pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Enterprise Sales",
        )
        active_names = set(
            pipeline.stages.filter(is_active=True).values_list(
                "name",
                flat=True,
            )
        )
        self.assertTrue(
            {
                "New leads",
                "Qualified",
                "Nurturing",
                "Average lead",
                "Ultra Hot",
                "Lead Won",
                "DNP",
                "Lead Lost",
            }.issubset(active_names)
        )

    def test_stage_configuration_cannot_deactivate_occupied_stage(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
        )
        move_lead_to_stage(
            lead=self.lead,
            stage=self.review_stage,
            actor=self.admin,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "upsert_stage_configuration",
                {
                    "pipeline_id": str(self.pipeline.id),
                    "stage_id": str(self.review_stage.id),
                    "data": {"is_active": False},
                    "reason": "Attempt occupied stage deactivation",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.review_stage.refresh_from_db()
        self.assertTrue(self.review_stage.is_active)

    def test_attribute_configuration_uses_policy_dry_run_approval_and_service(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_CRM_CONFIG_WRITE,
            ],
            approval_required_capabilities=[CAP_CRM_CONFIG_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "data": {
                "name": "Company Size",
                "field_type": "numeric",
                "description": "Approximate employee count",
            },
            "reason": "Add qualification CRM attribute",
        }

        dry = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])
        self.assertEqual(dry["structuredContent"]["status"], "DRY_RUN")
        self.assertFalse(
            AttributeDefinition.objects.filter(
                organization=self.organization,
                name="Company Size",
            ).exists()
        )

        blocked = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
                {**arguments, "dry_run": False},
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        applied = self._result(
            self._call(
                bearer,
                "upsert_attribute_configuration",
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
        self.assertFalse(applied["isError"])
        self.assertEqual(applied["structuredContent"]["status"], "FIXED")
        approval_use = OperationsApprovalUse.objects.get(
            approval_event_id=dry["structuredContent"]["approval_event_id"]
        )
        with self.assertRaises(ValidationError):
            OperationsApprovalUse.objects.filter(
                pk=approval_use.pk
            ).update(created_at=timezone.now())
        with self.assertRaises(ValidationError):
            OperationsApprovalUse.objects.filter(
                pk=approval_use.pk
            ).delete()

        replacement_use = OperationsApprovalUse(
            id=approval_use.id,
            approval_event=approval_use.approval_event,
        )
        with self.assertRaises(ValidationError):
            OperationsApprovalUse.objects.bulk_create(
                [replacement_use],
                update_conflicts=True,
                update_fields=["created_at"],
                unique_fields=["id"],
            )
        definition = AttributeDefinition.objects.get(
            organization=self.organization,
            name="Company Size",
        )
        self.assertEqual(definition.key, "company_size")
        self.assertEqual(definition.field_type, "numeric")
