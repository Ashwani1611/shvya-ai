# ruff: noqa: F403,F405
"""Domain-focused coverage split from the historical Operations MCP suite."""

from apps.integrations.tests.operations_mcp_test_base import *


class TestOperationsMCPSuperadmin(OperationsMCPBase):
    def test_org_admin_is_tenant_scoped_and_cannot_switch_context(self):
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
        own = self._result(
            self._call(
                bearer,
                "get_lead_snapshot",
                {"lead_id": str(self.lead.id)},
            )
        )
        self.assertFalse(own["isError"])
        self.assertEqual(own["structuredContent"]["id"], str(self.lead.id))

        foreign = self._result(
            self._call(
                bearer,
                "get_lead_snapshot",
                {"lead_id": str(self.other_lead.id)},
            )
        )
        self.assertTrue(foreign["isError"])
        self.assertNotIn(self.other_lead.name, json.dumps(foreign))

        switch = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.other_organization.id),
                    "reason": "Attempt tenant switch",
                },
            )
        )
        self.assertTrue(switch["isError"])
        self.assertEqual(switch["structuredContent"]["status"], "NOT_ALLOWED")

    def test_superadmin_context_switch_audits_exit_without_cross_tenant_metadata(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[
                OPERATIONS_READ_SCOPE,
                OPERATIONS_WRITE_SCOPE,
            ],
        )
        first = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Investigate first organization",
                },
            )
        )
        self.assertFalse(first["isError"])
        first_session = OperationsSupportSession.objects.get(
            pk=first["structuredContent"][
                "support_context_id"
            ]
        )

        second = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.other_organization.id),
                    "reason": "Investigate second organization",
                },
            )
        )
        self.assertFalse(second["isError"])
        first_session.refresh_from_db()
        self.assertIsNotNone(first_session.ended_at)

        exit_event = OperationsAuditEvent.objects.get(
            organization=self.organization,
            tool_name="support_context_end_on_switch",
            support_session=first_session,
        )
        self.assertEqual(
            exit_event.change_summary,
            {
                "support_context": "ended",
                "cause": "organization_switch",
            },
        )
        exit_blob = json.dumps(
            {
                "reason": exit_event.reason,
                "summary": exit_event.change_summary,
            }
        )
        self.assertNotIn(
            str(self.other_organization.id),
            exit_blob,
        )
        self.assertNotIn(
            self.other_organization.name,
            exit_blob,
        )

        second_audit = (
            OperationsAuditEvent.objects.filter(
                organization=self.other_organization,
                tool_name="select_organization_context",
            )
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(second_audit)
        second_blob = json.dumps(
            {
                "reason": second_audit.reason,
                "summary": second_audit.change_summary,
            }
        )
        self.assertNotIn(
            str(self.organization.id),
            second_blob,
        )
        self.assertNotIn(
            str(first_session.id),
            second_blob,
        )
        self.assertNotIn(
            self.organization.name,
            second_blob,
        )

    def test_superadmin_platform_tool_audit_is_not_attributed_to_selected_tenant(self):
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
                    "reason": "Select customer before platform audit test",
                },
            )
        )
        self.assertFalse(selected["isError"])

        listed = self._result(
            self._call(
                bearer,
                "list_organizations",
                {"limit": 5},
            )
        )
        self.assertFalse(listed["isError"])

        audit = (
            OperationsAuditEvent.objects.filter(
                actor=self.superadmin,
                tool_name="list_organizations",
            )
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(audit)
        self.assertIsNone(audit.organization_id)
        self.assertIsNone(audit.support_session_id)

        tenant_audits = OperationsAuditEvent.objects.filter(
            organization=self.organization,
            tool_name="list_organizations",
        )
        self.assertFalse(tenant_audits.exists())

    def test_superadmin_org_discovery_distinguishes_open_from_recent_support(self):
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
                    "reason": "Review support presence semantics",
                },
            )
        )
        self.assertFalse(selected["isError"])

        support = OperationsSupportSession.objects.get(
            token__actor=self.superadmin,
            organization=self.organization,
            ended_at__isnull=True,
        )
        OperationsSupportSession.objects.filter(
            pk=support.pk
        ).update(
            last_seen_at=timezone.now() - timedelta(minutes=16),
        )

        listed = self._result(
            self._call(
                bearer,
                "list_organizations",
                {
                    "query": self.organization.name,
                    "limit": 10,
                },
            )
        )
        self.assertFalse(listed["isError"])
        row = next(
            item
            for item in listed["structuredContent"]["organizations"]
            if item["id"] == str(self.organization.id)
        )
        self.assertTrue(
            row["superadmin_support_context_open"]
        )
        self.assertFalse(
            row["superadmin_support_recently_active"]
        )
        self.assertFalse(
            row["superadmin_support_active"]
        )

        context = self._result(
            self._call(
                bearer,
                "get_operations_context",
            )
        )
        self.assertFalse(context["isError"])
        support_context = context["structuredContent"][
            "superadmin_support_context"
        ]
        self.assertIsNotNone(support_context)
        self.assertFalse(
            support_context["recently_active"]
        )
        self.assertIsNotNone(
            support_context["last_seen_at"]
        )

    def test_superadmin_customer_tools_fail_closed_if_selected_org_is_disabled(self):
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
                    "reason": "Investigate organization before disable",
                },
            )
        )
        self.assertFalse(selected["isError"])

        self.organization.is_active = False
        self.organization.save(update_fields=["is_active", "updated_at"])

        blocked = self._result(
            self._call(
                bearer,
                "get_organization_configuration",
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertIn(
            "organization is disabled",
            blocked["structuredContent"]["error"].lower(),
        )

        context = self._result(
            self._call(bearer, "get_operations_context")
        )
        self.assertFalse(context["isError"])
        self.assertFalse(
            context["structuredContent"]["organization"]["active"]
        )

        support_session = OperationsSupportSession.objects.get(
            token__actor=self.superadmin,
            organization=self.organization,
            ended_at__isnull=True,
        )
        cleared = self._result(
            self._call(
                bearer,
                "clear_organization_context",
                {"reason": "Leave disabled organization context"},
            )
        )
        self.assertFalse(cleared["isError"])
        self.assertEqual(
            cleared["structuredContent"]["status"],
            "cleared",
        )
        support_session.refresh_from_db()
        self.assertIsNotNone(support_session.ended_at)

        audit = OperationsAuditEvent.objects.get(
            actor=self.superadmin,
            tool_name="clear_organization_context",
        )
        self.assertEqual(
            audit.organization_id,
            self.organization.id,
        )
        self.assertEqual(
            audit.support_session_id,
            support_session.id,
        )

    def test_operations_rejects_secret_like_action_reasons_before_persistence(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )

        blocked_context = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "password: support-secret",
                },
            )
        )
        self.assertTrue(blocked_context["isError"])
        self.assertEqual(
            blocked_context["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertFalse(
            OperationsSupportSession.objects.filter(
                token__actor=self.superadmin,
                organization=self.organization,
                ended_at__isnull=True,
            ).exists()
        )

        blocked_opaque = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": (
                        "Review credential "
                        + ("A" * 64)
                    ),
                },
            )
        )
        self.assertTrue(blocked_opaque["isError"])
        self.assertEqual(
            blocked_opaque["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.assertFalse(
            OperationsSupportSession.objects.filter(
                token__actor=self.superadmin,
                organization=self.organization,
                ended_at__isnull=True,
            ).exists()
        )

        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Review organization configuration",
                },
            )
        )
        self.assertFalse(selected["isError"])

        blocked_write = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "api_key: mutation-secret",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(blocked_write["isError"])
        self.assertEqual(
            blocked_write["structuredContent"]["status"],
            "NOT_ALLOWED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

        payload = json.dumps(blocked_write)
        self.assertNotIn("mutation-secret", payload)

    def test_operations_rejects_generic_action_reasons_before_persistence(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[
                OPERATIONS_READ_SCOPE,
                OPERATIONS_WRITE_SCOPE,
            ],
        )

        blocked_context = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Fixing issue",
                },
            )
        )
        self.assertTrue(blocked_context["isError"])
        self.assertEqual(
            blocked_context["structuredContent"]["status"],
            "FAILED",
        )
        self.assertIn(
            "specific operational reason",
            blocked_context["structuredContent"]["error"],
        )
        self.assertFalse(
            OperationsSupportSession.objects.filter(
                token__actor=self.superadmin,
                organization=self.organization,
                ended_at__isnull=True,
            ).exists()
        )

        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Review one lead stage transition",
                },
            )
        )
        self.assertFalse(selected["isError"])

        blocked_write = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "User asked me to.",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(blocked_write["isError"])
        self.assertEqual(
            blocked_write["structuredContent"]["status"],
            "FAILED",
        )
        self.assertIn(
            "specific operational reason",
            blocked_write["structuredContent"]["error"],
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.stage_id,
            self.new_stage.id,
        )

    def test_superadmin_customer_writes_still_require_bound_approval_receipt(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
            scopes=[
                OPERATIONS_READ_SCOPE,
                OPERATIONS_WRITE_SCOPE,
            ],
        )
        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Review one customer stage transition",
                },
            )
        )
        self.assertFalse(selected["isError"])

        arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Move reviewed lead to review stage",
        }
        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **arguments,
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(dry["isError"])
        self.assertTrue(
            dry["structuredContent"]["approval_required"]
        )
        approval_event_id = dry["structuredContent"][
            "approval_event_id"
        ]

        no_approval = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **arguments,
                    "dry_run": False,
                },
            )
        )
        self.assertTrue(no_approval["isError"])
        self.assertEqual(
            no_approval["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        no_receipt = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                },
            )
        )
        self.assertTrue(no_receipt["isError"])
        self.assertEqual(
            no_receipt["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        applied = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": approval_event_id,
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
            self.lead.stage_id,
            self.review_stage.id,
        )

    def test_superadmin_requires_explicit_context_and_creates_support_session(self):
        bearer = self._token(
            actor=self.superadmin,
            role=ROLE_SUPERADMIN,
        )
        before = self._result(
            self._call(bearer, "get_organization_configuration")
        )
        self.assertTrue(before["isError"])
        self.assertIn(
            "Select an organization support context",
            before["structuredContent"]["error"],
        )

        selected = self._result(
            self._call(
                bearer,
                "select_organization_context",
                {
                    "organization_id": str(self.organization.id),
                    "reason": "Investigate qualification issue",
                },
            )
        )
        self.assertFalse(selected["isError"])
        self.assertEqual(
            selected["structuredContent"]["organization"]["id"],
            str(self.organization.id),
        )
        session = OperationsSupportSession.objects.get(ended_at__isnull=True)
        self.assertEqual(session.organization_id, self.organization.id)
        self.assertEqual(session.actor_id, self.superadmin.id)

        configured = self._result(
            self._call(bearer, "get_organization_configuration")
        )
        self.assertFalse(configured["isError"])
        self.assertEqual(
            configured["structuredContent"]["organization"]["id"],
            str(self.organization.id),
        )

    def test_write_policy_enforces_dry_run_approval_execute_and_verify(self):
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
        base_arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Move lead to reviewed sales stage",
        }

        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**base_arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])
        self.assertEqual(dry["structuredContent"]["status"], "DRY_RUN")
        approval_event_id = dry["structuredContent"]["approval_event_id"]
        self.assertIn(
            approval_event_id,
            dry["content"][0]["text"],
        )
        self.assertEqual(
            dry["structuredContent"]["approval_expires_in_seconds"],
            1800,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

        blocked = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**base_arguments, "dry_run": False},
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        wrong_receipt = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **base_arguments,
                    "target_stage_id": str(self.new_stage.id),
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(wrong_receipt["isError"])
        self.assertEqual(
            wrong_receipt["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        applied = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **base_arguments,
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
        self.assertEqual(
            applied["structuredContent"]["verification"],
            "passed",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.review_stage.id)
        self.assertTrue(
            LeadActivity.objects.filter(
                lead=self.lead,
                organization=self.organization,
                topic=LeadActivity.Topic.STAGE_CHANGED,
                new_stage=self.review_stage,
            ).exists()
        )

        replayed_approval = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **base_arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": dry["structuredContent"][
                        "approval_event_id"
                    ],
                },
            )
        )
        self.assertTrue(replayed_approval["isError"])
        self.assertEqual(
            replayed_approval["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )

        outcomes = list(
            OperationsAuditEvent.objects.filter(
                organization=self.organization,
                tool_name="move_lead_stage",
            ).values_list("outcome", flat=True)
        )
        self.assertIn(OperationsAuditEvent.Outcome.DRY_RUN, outcomes)
        self.assertIn(
            OperationsAuditEvent.Outcome.APPROVAL_REQUIRED,
            outcomes,
        )
        self.assertIn(OperationsAuditEvent.Outcome.SUCCESS, outcomes)

    def test_approval_receipt_without_proposal_digest_fails_closed(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[
                CAP_LEAD_STAGE_WRITE,
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
        )
        arguments = {
            "lead_id": str(self.lead.id),
            "target_stage_id": str(self.review_stage.id),
            "reason": "Apply reviewed legacy approval receipt",
        }
        legacy = OperationsAuditEvent.objects.create(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            tool_name="move_lead_stage",
            capability=CAP_LEAD_STAGE_WRITE,
            target_type="lead",
            target_id=str(self.lead.id),
            reason=arguments["reason"],
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            request_fingerprint=approval_fingerprint(arguments),
            change_summary={
                "operation": "move_lead_stage",
            },
        )

        blocked = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    **arguments,
                    "dry_run": False,
                    "approved": True,
                    "approval_event_id": str(legacy.id),
                },
            )
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            blocked["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.assertIn(
            "proposal evidence",
            blocked["structuredContent"]["error"],
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_operations_approval_receipt_expires(self):
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
            "reason": "Review time-bounded stage change",
        }
        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {**arguments, "dry_run": True},
            )
        )
        self.assertFalse(dry["isError"])
        event = OperationsAuditEvent.objects.get(
            pk=dry["structuredContent"]["approval_event_id"]
        )

        future = event.created_at + timedelta(minutes=31)
        with patch(
            "apps.integrations.operations_tools.timezone.now",
            return_value=future,
        ):
            expired = self._result(
                self._call(
                    bearer,
                    "move_lead_stage",
                    {
                        **arguments,
                        "dry_run": False,
                        "approved": True,
                        "approval_event_id": str(event.id),
                    },
                )
            )
        self.assertTrue(expired["isError"])
        self.assertEqual(
            expired["structuredContent"]["status"],
            "APPROVAL_REQUIRED",
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)
