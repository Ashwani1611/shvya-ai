# ruff: noqa: F403,F405
"""Domain-focused coverage split from the historical Operations MCP suite."""

from apps.integrations.tests.operations_mcp_test_base import *


class TestOperationsMCPQualificationAudit(OperationsMCPBase):
    def test_qualification_diagnosis_ignores_foreign_account_processing_markers(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        own_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Qualification Evidence Sender",
            display_phone_number="+919000000080",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        foreign_account = WhatsAppAccount.objects.create(
            organization=self.other_organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Foreign Qualification Evidence Sender",
            display_phone_number="+919000000081",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=own_account,
            lead=self.lead,
            external_id="qualification-evidence-own",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number=self.lead.phone,
            to_number="+919000000080",
            body="own qualification evidence",
            raw_payload={
                "shvya_ai_processing": {
                    "processed": True,
                    "qualification_execution_status": "own_completed",
                    "qualification_reconciliation_status": "own_reconciled",
                }
            },
        )
        foreign_message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=foreign_account,
            lead=self.lead,
            external_id="qualification-evidence-foreign",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number=self.lead.phone,
            to_number="+919000000081",
            body="foreign qualification evidence",
            raw_payload={
                "shvya_ai_processing": {
                    "processed": True,
                    "qualification_execution_status": "foreign_completed",
                    "qualification_reconciliation_status": "foreign_reconciled",
                }
            },
        )
        WhatsAppMessage.objects.filter(
            pk=foreign_message.pk
        ).update(
            created_at=timezone.now()
            + timedelta(seconds=1)
        )

        snapshot = (
            {"mode": "guided", "flow_version": "evidence-flow"},
            [],
            {
                "qualification_status": "in_progress",
                "qualification_result": "",
                "all_requirements_answered": False,
                "answered_requirement_ids": [],
                "missing_requirement_ids": [],
                "flow_version": "evidence-flow",
            },
            {"errors": []},
            {
                "qualified": False,
                "reason": "criteria_evaluated",
                "rules": [],
            },
            None,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        with patch(
            "apps.integrations.operations_tools._qualification_contract_snapshot",
            return_value=snapshot,
        ):
            result = self._result(
                self._call(
                    bearer,
                    "diagnose_lead_qualification",
                    {"lead_id": str(self.lead.id)},
                )
            )

        self.assertFalse(result["isError"])
        markers = result["structuredContent"][
            "latest_processing_markers"
        ]
        self.assertEqual(
            markers["qualification_execution_status"],
            "own_completed",
        )
        self.assertEqual(
            markers["qualification_reconciliation_status"],
            "own_reconciled",
        )
        payload = json.dumps(result["structuredContent"])
        self.assertNotIn("foreign_completed", payload)
        self.assertNotIn("foreign_reconciled", payload)
        self.assertNotIn(str(foreign_account.id), payload)
    def test_operations_audit_reports_explicit_truncation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUDIT_READ,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        for _index in range(2):
            context = self._result(
                self._call(
                    bearer,
                    "get_operations_context",
                    {},
                )
            )
            self.assertFalse(context["isError"])

        result = self._result(
            self._call(
                bearer,
                "get_operations_audit",
                {"limit": 1},
            )
        )
        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["events_returned"], 1)
        self.assertGreaterEqual(data["event_count"], 2)
        self.assertTrue(data["events_truncated"])

    def test_qualification_diagnosis_completed_but_not_qualified_is_not_failure(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        snapshot = (
            {"mode": "guided", "flow_version": "test-flow"},
            [{"id": "budget", "question": "What is your budget?", "required": True}],
            {
                "qualification_status": "completed",
                "qualification_result": "",
                "all_requirements_answered": True,
                "answered_requirement_ids": ["budget"],
                "missing_requirement_ids": [],
                "flow_version": "test-flow",
            },
            {"errors": []},
            {
                "qualified": False,
                "reason": "criteria_evaluated",
                "rules": [{"verdict": "fail"}],
            },
            None,
        )

        with patch(
            "apps.integrations.operations_tools._qualification_contract_snapshot",
            return_value=snapshot,
        ):
            result = self._result(
                self._call(
                    bearer,
                    "diagnose_lead_qualification",
                    {"lead_id": str(self.lead.id)},
                )
            )

        self.assertFalse(result["isError"])
        diagnosis = result["structuredContent"]
        self.assertEqual(
            diagnosis["classification"],
            "NO_PROBLEM_FOUND",
        )
        self.assertFalse(diagnosis["repair_available"])
        self.assertFalse(
            diagnosis["qualification"]["criteria_qualified"]
        )
        self.assertIn(
            "transition is not expected",
            diagnosis["root_cause"],
        )

    def test_qualification_repair_supports_authoritative_cross_pipeline_target(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[],
        )
        target_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Qualified Pipeline",
        )
        target_stage = target_pipeline.stages.get(name="Qualified")
        target = {
            "id": str(target_stage.id),
            "name": target_stage.name,
            "pipeline_id": str(target_pipeline.id),
            "pipeline__name": target_pipeline.name,
        }
        snapshot = (
            {"mode": "guided", "flow_version": "repair-flow"},
            [],
            {
                "qualification_status": "completed",
                "qualification_result": "qualified",
                "all_requirements_answered": True,
                "answered_requirement_ids": [],
                "missing_requirement_ids": [],
                "flow_version": "repair-flow",
            },
            {"errors": [], "completion_stage": target},
            {
                "qualified": True,
                "reason": "criteria_evaluated",
                "rules": [],
            },
            target,
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )

        with patch(
            "apps.integrations.operations_tools._qualification_contract_snapshot",
            return_value=snapshot,
        ):
            dry = self._result(
                self._call(
                    bearer,
                    "repair_qualification_stage",
                    {
                        "lead_id": str(self.lead.id),
                        "reason": "Repair verified qualification completion",
                        "dry_run": True,
                    },
                )
            )
            self.assertFalse(dry["isError"])
            self.assertEqual(
                dry["structuredContent"]["status"],
                "DRY_RUN",
            )
            proposed = dry["structuredContent"]["proposed_change"]
            self.assertEqual(
                proposed["after"]["pipeline_id"],
                str(target_pipeline.id),
            )
            self.assertEqual(
                proposed["after"]["stage_id"],
                str(target_stage.id),
            )

            applied = self._result(
                self._call(
                    bearer,
                    "repair_qualification_stage",
                    {
                        "lead_id": str(self.lead.id),
                        "reason": "Repair verified qualification completion",
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
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.pipeline_id,
            target_pipeline.id,
        )
        self.assertEqual(
            self.lead.stage_id,
            target_stage.id,
        )

    def test_lead_attribute_approval_is_invalid_after_values_change(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_ATTRIBUTES_WRITE,
            ],
            approval_required_capabilities=[CAP_LEAD_ATTRIBUTES_WRITE],
        )
        definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Company Size",
            key="company_size",
            field_type=AttributeDefinition.FieldType.NUMERIC,
        )
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

        self.lead.attributes = {
            **(self.lead.attributes or {}),
            definition.key: "40",
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
            "40",
        )

    def test_pipeline_config_approval_is_invalid_after_pipeline_changes(self):
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
            "data": {
                "name": self.pipeline.name,
                "description": "Approved description",
                "is_active": True,
                "ai_enabled": True,
            },
            "reason": "Update reviewed pipeline description",
        }
        dry = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        self.pipeline.description = "Newer manual description"
        self.pipeline.save(update_fields=["description", "updated_at"])

        stale = self._result(
            self._call(
                bearer,
                "upsert_pipeline_configuration",
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
        self.pipeline.refresh_from_db()
        self.assertEqual(
            self.pipeline.description,
            "Newer manual description",
        )

    def test_operations_stage_move_cannot_bypass_backend_qualification(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
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
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.qualified.id),
                    "reason": "Attempt direct Qualified transition",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_sensitive_target_requirement_returns_manual_fix_required(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[],
        )
        sensitive = AttributeDefinition.objects.create(
            organization=self.organization,
            name="API Token",
            key="api_token",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.review_stage.config = {
            "required_attribute_ids": [str(sensitive.id)],
        }
        self.review_stage.save(update_fields=["config", "updated_at"])

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "Attempt stage with sensitive requirement",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "MANUAL_FIX_REQUIRED",
        )
        self.assertNotIn(
            "API Token",
            result["structuredContent"]["error"],
        )
        self.assertIn(
            "sensitive required CRM data",
            result["structuredContent"]["error"],
        )

    def test_operations_stage_move_enforces_target_required_attributes(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[],
        )
        required = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Company Size",
            key="company_size",
            field_type=AttributeDefinition.FieldType.NUMERIC,
        )
        self.review_stage.config = {
            "required_attribute_ids": [str(required.id)],
        }
        self.review_stage.save(update_fields=["config", "updated_at"])

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Move after required CRM data is complete",
            "dry_run": True,
        }

        blocked = self._result(
            self._call(bearer, "move_lead_stage", arguments)
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "Company Size",
            blocked["structuredContent"]["error"],
        )

        self.lead.attributes = {
            **(self.lead.attributes or {}),
            "company_size": "25",
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

        allowed = self._result(
            self._call(bearer, "move_lead_stage", arguments)
        )
        self.assertFalse(allowed["isError"])
        self.assertEqual(
            allowed["structuredContent"]["status"],
            "DRY_RUN",
        )

    def test_operations_stage_approval_is_invalid_after_lead_state_changes(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[CAP_LEAD_STAGE_WRITE],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Approve move from current lead stage",
        }
        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])

        move_lead_to_stage(
            lead=self.lead,
            stage=self.followup_stage,
            actor=self.admin,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.followup_stage.id)

        stale = self._result(
            self._call(
                bearer,
                "move_lead_stage",
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
        self.assertEqual(self.lead.stage_id, self.followup_stage.id)

    def test_approved_true_cannot_bypass_disabled_write_capability(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        result = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.qualified.id),
                    "reason": "Unauthorized stage change request",
                    "dry_run": False,
                    "approved": True,
                },
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "SUPERADMIN_REQUIRED",
        )
        self.assertIn(
            "SHVYA Superadmin",
            result["structuredContent"]["error"],
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_operations_audit_lookup_supports_exact_tenant_scoped_filters(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUDIT_READ,
            ],
        )
        own = OperationsAuditEvent.objects.create(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            tool_name="move_lead_stage",
            capability=CAP_LEAD_STAGE_WRITE,
            target_type="lead",
            target_id=str(self.lead.id),
            reason="Trace this exact action",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="d" * 64,
            change_summary={"verification": "passed"},
        )
        foreign = OperationsAuditEvent.objects.create(
            actor=self.other_admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.other_organization,
            tool_name="move_lead_stage",
            capability=CAP_LEAD_STAGE_WRITE,
            target_type="lead",
            target_id=str(self.other_lead.id),
            reason="Foreign action",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="e" * 64,
            change_summary={"verification": "passed"},
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        exact = self._result(
            self._call(
                bearer,
                "get_operations_audit",
                {
                    "audit_event_id": str(own.id),
                    "limit": 10,
                },
            )
        )
        self.assertFalse(exact["isError"])
        self.assertEqual(
            exact["structuredContent"]["count"],
            1,
        )
        self.assertEqual(
            exact["structuredContent"]["events"][0]["id"],
            str(own.id),
        )

        resource = self._result(
            self._call(
                bearer,
                "get_operations_audit",
                {
                    "tool_name": "move_lead_stage",
                    "target_type": "lead",
                    "target_id": str(self.lead.id),
                    "outcome": "success",
                },
            )
        )
        self.assertFalse(resource["isError"])
        self.assertEqual(
            [row["id"] for row in resource["structuredContent"]["events"]],
            [str(own.id)],
        )

        foreign_lookup = self._result(
            self._call(
                bearer,
                "get_operations_audit",
                {
                    "audit_event_id": str(foreign.id),
                },
            )
        )
        self.assertFalse(foreign_lookup["isError"])
        self.assertEqual(
            foreign_lookup["structuredContent"]["events"],
            [],
        )
        self.assertNotIn(
            "Foreign action",
            json.dumps(foreign_lookup),
        )

    def test_superadmin_platform_audit_scope_never_mixes_customer_events(self):
        platform_event = OperationsAuditEvent.objects.create(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            organization=None,
            tool_name="list_organizations",
            capability=CAP_ORGANIZATION_READ,
            target_type="platform",
            target_id="",
            reason="Review platform organizations",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="p" * 64,
            change_summary={"result_count": 2},
        )
        tenant_event = OperationsAuditEvent.objects.create(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            tool_name="get_organization_configuration",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(self.organization.id),
            reason="Tenant-only audit event",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="t" * 64,
            change_summary={"configured": True},
        )

        super_bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        result = self._result(
            self._call(
                super_bearer,
                "get_operations_audit",
                {
                    "scope": "platform",
                    "limit": 20,
                },
            )
        )
        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        self.assertEqual(data["scope"], "platform")
        self.assertIsNone(data["organization"])
        self.assertIn(
            str(platform_event.id),
            [row["id"] for row in data["events"]],
        )
        self.assertNotIn(
            str(tenant_event.id),
            [row["id"] for row in data["events"]],
        )
        payload = json.dumps(data)
        self.assertNotIn("Tenant-only audit event", payload)

    def test_org_admin_cannot_request_platform_operations_audit(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUDIT_READ,
            ],
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
                "get_operations_audit",
                {"scope": "platform"},
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "Superadmin",
            result["structuredContent"]["error"],
        )

    def test_operations_audit_relationships_are_protected_from_deletion(self):
        protected_actor = User.objects.create_superuser(
            email="protected-audit-actor@example.test",
            password=None,
            name="Protected Audit Actor",
        )
        event = OperationsAuditEvent.objects.create(
            actor=protected_actor,
            role=ROLE_SUPERADMIN,
            organization=self.organization,
            tool_name="protected_event",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(self.organization.id),
            reason="Preserve immutable audit identity",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="c" * 64,
            change_summary={"status": "preserved"},
        )

        with self.assertRaises(ProtectedError):
            protected_actor.delete()

        event.refresh_from_db()
        self.assertEqual(event.actor_id, protected_actor.id)

    def test_operations_audit_events_are_append_only_even_through_queryset(self):
        event = OperationsAuditEvent.objects.create(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            organization=self.organization,
            tool_name="test_tool",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(self.organization.id),
            reason="Verify immutable audit storage",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="a" * 64,
            change_summary={"status": "verified"},
        )

        event.reason = "Attempted rewrite"
        with self.assertRaises(ValidationError):
            event.save(update_fields=["reason"])

        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.filter(pk=event.pk).update(
                reason="Bulk rewrite"
            )

        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.filter(pk=event.pk).delete()

        event.reason = "Bulk update attempt"
        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.bulk_update(
                [event],
                ["reason"],
            )

        conflict = OperationsAuditEvent(
            id=event.id,
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            organization=self.organization,
            tool_name="replacement",
            outcome=OperationsAuditEvent.Outcome.ERROR,
            request_fingerprint="b" * 64,
        )
        with self.assertRaises(ValidationError):
            OperationsAuditEvent.objects.bulk_create(
                [conflict],
                update_conflicts=True,
                update_fields=["tool_name", "outcome"],
                unique_fields=["id"],
            )

        event.refresh_from_db()
        self.assertEqual(event.reason, "Verify immutable audit storage")
        self.assertEqual(event.tool_name, "test_tool")

    def test_audit_stores_argument_fingerprint_not_raw_query(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        query = "+919999999991"
        result = self._result(
            self._call(bearer, "find_leads", {"query": query})
        )
        self.assertFalse(result["isError"])

        event = OperationsAuditEvent.objects.get(
            organization=self.organization,
            tool_name="find_leads",
        )
        self.assertEqual(len(event.request_fingerprint), 64)
        self.assertNotIn(query, event.request_fingerprint)
        self.assertNotIn(query, json.dumps(event.change_summary))
