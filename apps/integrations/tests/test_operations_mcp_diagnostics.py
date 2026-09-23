# ruff: noqa: F403,F405
"""Domain-focused coverage split from the historical Operations MCP suite."""

from apps.integrations.tests.operations_mcp_test_base import *


class TestOperationsMCPDiagnostics(OperationsMCPBase):
    def test_operations_fail_closed_on_corrupted_lead_pipeline_stage_links(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
                CAP_LEAD_STAGE_WRITE,
            ],
            approval_required_capabilities=[
                CAP_LEAD_STAGE_WRITE,
            ],
        )
        corrupted = Lead.objects.create(
            organization=self.organization,
            pipeline=self.other_lead.pipeline,
            stage=self.other_lead.stage,
            name="Corrupt Foreign Pipeline Lead",
            phone="+919999999993",
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

        found = self._result(
            self._call(
                bearer,
                "find_leads",
                {
                    "query": "Corrupt Foreign Pipeline Lead",
                    "limit": 10,
                },
            )
        )
        self.assertFalse(found["isError"])
        self.assertEqual(found["structuredContent"]["count"], 0)

        snapshot = self._result(
            self._call(
                bearer,
                "get_lead_snapshot",
                {"lead_id": str(corrupted.id)},
            )
        )
        self.assertTrue(snapshot["isError"])
        self.assertEqual(
            snapshot["structuredContent"]["status"],
            "FAILED",
        )

        blocked_move = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(corrupted.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "Review corrupt lead tenant relationship",
                    "dry_run": True,
                },
            )
        )
        self.assertTrue(blocked_move["isError"])
        self.assertEqual(
            blocked_move["structuredContent"]["status"],
            "FAILED",
        )

        payload = json.dumps(
            {
                "found": found,
                "snapshot": snapshot,
                "blocked_move": blocked_move,
            }
        )
        self.assertNotIn(
            self.other_organization.name,
            payload,
        )
        self.assertNotIn(
            self.other_lead.pipeline.name,
            payload,
        )
        self.assertNotIn(
            self.other_lead.stage.name,
            payload,
        )

        dry = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "Move reviewed valid lead to review stage",
                    "dry_run": True,
                },
            )
        )
        self.assertFalse(dry["isError"])

        Lead.objects.filter(pk=self.lead.pk).update(
            pipeline=self.other_lead.pipeline,
            stage=self.other_lead.stage,
        )
        stale = self._result(
            self._call(
                bearer,
                "move_lead_stage",
                {
                    "lead_id": str(self.lead.id),
                    "target_stage_id": str(self.review_stage.id),
                    "reason": "Move reviewed valid lead to review stage",
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
        stale_payload = json.dumps(stale)
        self.assertNotIn(
            self.other_organization.name,
            stale_payload,
        )
        self.assertNotIn(
            self.other_lead.pipeline.name,
            stale_payload,
        )
        self.assertNotIn(
            self.other_lead.stage.name,
            stale_payload,
        )

    def test_diagnostics_fail_closed_on_corrupted_cross_tenant_message_links(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Corruption Guard Sender",
            display_phone_number="+919000000050",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        corrupted = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.other_lead,
            external_id="cross-tenant-corrupt-message",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.FAILED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919111111111",
            to_number="+919000000050",
            body="foreign-linked message",
            error="foreign-linked failure",
        )
        leadless = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=None,
            external_id="leadless-own-failure",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.FAILED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919122222222",
            to_number="+919000000050",
            body="leadless own message",
            error="leadless own failure",
        )
        foreign_account = WhatsAppAccount.objects.create(
            organization=self.other_organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Foreign Corruption Sender",
            display_phone_number="+919000000051",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        corrupted_account_message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=foreign_account,
            lead=self.lead,
            external_id="cross-tenant-corrupt-account-message",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.FAILED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919133333333",
            to_number="+919000000051",
            body="foreign account linked message",
            error="foreign account linked failure",
        )
        foreign_instagram = InstagramAccount.objects.create(
            organization=self.other_organization,
            ig_user_id="foreign-diagnostic-account",
            username="foreign_diagnostic_account",
            status=InstagramAccount.Status.CONNECTED,
        )
        foreign_conversation = InstagramConversation.objects.create(
            organization=self.other_organization,
            account=foreign_instagram,
            lead=self.other_lead,
            participant_id="foreign-diagnostic-participant",
        )
        corrupted_instagram = InstagramMessage.objects.create(
            organization=self.organization,
            account=foreign_instagram,
            conversation=foreign_conversation,
            external_id="cross-tenant-corrupt-instagram-message",
            direction=InstagramMessage.Direction.INBOUND,
            status=InstagramMessage.Status.FAILED,
            message_type=InstagramMessage.MessageType.TEXT,
            body="foreign instagram linked message",
            error="foreign instagram linked failure",
        )
        hosted_source = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            external_id="cross-tenant-hosted-source",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919144444444",
            to_number="+919000000050",
            body="hosted source",
        )
        corrupted_hosted = HostedAutomationJob.objects.create(
            organization=self.organization,
            account=foreign_account,
            lead=self.lead,
            source_message=hosted_source,
            available_at=timezone.now(),
            status=HostedAutomationJob.Status.FAILED,
            result={"reason": "foreign_account_relation"},
            error="foreign hosted relation",
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        traced = self._result(
            self._call(
                bearer,
                "trace_message",
                {
                    "message_id": str(corrupted.id),
                },
            )
        )
        self.assertTrue(traced["isError"])
        self.assertEqual(
            traced["structuredContent"]["status"],
            "FAILED",
        )
        trace_blob = json.dumps(traced)
        self.assertNotIn(
            str(self.other_lead.id),
            trace_blob,
        )
        self.assertNotIn(
            self.other_lead.name,
            trace_blob,
        )

        recent = self._result(
            self._call(
                bearer,
                "get_recent_errors",
                {
                    "hours": 24,
                    "limit": 20,
                },
            )
        )
        self.assertFalse(recent["isError"])
        message_ids = {
            row["message_id"]
            for row in recent["structuredContent"][
                "whatsapp"
            ]
        }
        self.assertIn(str(leadless.id), message_ids)
        self.assertNotIn(str(corrupted.id), message_ids)
        self.assertNotIn(
            str(corrupted_account_message.id),
            message_ids,
        )
        instagram_message_ids = {
            row["message_id"]
            for row in recent["structuredContent"][
                "instagram"
            ]
        }
        self.assertNotIn(
            str(corrupted_instagram.id),
            instagram_message_ids,
        )
        hosted_job_ids = {
            row["job_id"]
            for row in recent["structuredContent"][
                "hosted"
            ]
        }
        self.assertNotIn(
            str(corrupted_hosted.id),
            hosted_job_ids,
        )
        recent_payload = json.dumps(
            recent["structuredContent"]
        )
        self.assertNotIn(
            str(self.other_lead.id),
            recent_payload,
        )
        self.assertNotIn(
            self.other_organization.name,
            recent_payload,
        )
        self.assertNotIn(
            "foreign account linked failure",
            recent_payload,
        )
        self.assertNotIn(
            "foreign instagram linked failure",
            recent_payload,
        )
        self.assertNotIn(
            "foreign hosted relation",
            recent_payload,
        )

        snapshot = self._result(
            self._call(
                bearer,
                "get_lead_snapshot",
                {"lead_id": str(self.lead.id)},
            )
        )
        self.assertFalse(snapshot["isError"])
        self.assertEqual(
            snapshot["structuredContent"][
                "whatsapp_message_count"
            ],
            1,
        )

        conversation = self._result(
            self._call(
                bearer,
                "get_conversation",
                {
                    "lead_id": str(self.lead.id),
                    "channel": "whatsapp",
                    "limit": 20,
                },
            )
        )
        self.assertFalse(conversation["isError"])
        conversation_external_ids = {
            row["external_id"]
            for row in conversation[
                "structuredContent"
            ]["messages"]
        }
        self.assertIn(
            hosted_source.external_id,
            conversation_external_ids,
        )
        self.assertNotIn(
            corrupted_account_message.external_id,
            conversation_external_ids,
        )

        runtime = self._result(
            self._call(
                bearer,
                "get_runtime_health",
                {},
            )
        )
        self.assertFalse(runtime["isError"])
        counts = runtime["structuredContent"]["counts"]
        self.assertEqual(counts["leads"], 1)
        self.assertEqual(
            counts["whatsapp_inbound_24h"],
            2,
        )
        self.assertEqual(
            counts["whatsapp_failed_24h"],
            1,
        )
        self.assertEqual(
            counts["instagram_failed_24h"],
            0,
        )
        self.assertEqual(
            counts["hosted_failed_24h"],
            0,
        )

    def test_workflow_trace_and_runtime_ignore_corrupt_run_relations(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        workflow = SmartTrigger.objects.create(
            organization=self.organization,
            name="Runtime Relation Guard Workflow",
            enabled=True,
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
            fingerprint="a" * 64,
            created_by=self.admin,
        )
        foreign_event = TriggerEvent.objects.create(
            organization=self.other_organization,
            lead=self.other_lead,
            kind="lead_updated",
            key="foreign-event-for-own-run",
            payload={"private": "foreign-event-payload"},
        )
        corrupt_run = TriggerRun.objects.create(
            rule=workflow,
            event=foreign_event,
            lead=self.lead,
            action_type="ai",
            action={"enabled": True},
            status="failed",
            detail="foreign-event-run-detail",
            due_at=timezone.now(),
            finished_at=timezone.now(),
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        trace = self._result(
            self._call(
                bearer,
                "get_workflow_trace",
                {
                    "lead_id": str(self.lead.id),
                    "limit": 20,
                },
            )
        )
        self.assertFalse(trace["isError"])
        trace_payload = json.dumps(
            trace["structuredContent"]
        )
        self.assertNotIn(
            str(corrupt_run.id),
            trace_payload,
        )
        self.assertNotIn(
            str(foreign_event.id),
            trace_payload,
        )
        self.assertNotIn(
            "foreign-event-run-detail",
            trace_payload,
        )
        self.assertNotIn(
            "foreign-event-payload",
            trace_payload,
        )

        runtime = self._result(
            self._call(
                bearer,
                "get_runtime_health",
                {},
            )
        )
        self.assertFalse(runtime["isError"])
        self.assertEqual(
            runtime["structuredContent"]["counts"][
                "workflow_failed_24h"
            ],
            0,
        )

    def test_diagnostic_lookup_inputs_are_length_bounded(self):
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
        tool_list = self._list_tools(bearer)
        definitions = {
            item["name"]: item
            for item in tool_list["tools"]
        }
        self.assertEqual(
            definitions["find_leads"]["inputSchema"][
                "properties"
            ]["query"]["maxLength"],
            255,
        )
        self.assertEqual(
            definitions["trace_message"]["inputSchema"][
                "properties"
            ]["message_id"]["maxLength"],
            255,
        )

        oversized = "x" * 256

        lead_search = self._result(
            self._call(
                bearer,
                "find_leads",
                {"query": oversized},
            )
        )
        self.assertTrue(lead_search["isError"])
        self.assertEqual(
            lead_search["structuredContent"]["status"],
            "FAILED",
        )
        self.assertNotIn(
            oversized,
            json.dumps(lead_search),
        )

        message_trace = self._result(
            self._call(
                bearer,
                "trace_message",
                {"message_id": oversized},
            )
        )
        self.assertTrue(message_trace["isError"])
        self.assertEqual(
            message_trace["structuredContent"]["status"],
            "FAILED",
        )
        self.assertNotIn(
            oversized,
            json.dumps(message_trace),
        )

    def test_bounded_diagnostic_reads_report_truncation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        for index in range(11):
            Lead.objects.create(
                organization=self.organization,
                pipeline=self.pipeline,
                stage=self.new_stage,
                name=f"Bounded Diagnostic Lead {index:02d}",
                phone=f"+918600000{index:03d}",
            )

        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Bounded Diagnostic Sender",
            display_phone_number="+919000000066",
            phone_number_id="bounded-diagnostic-sender",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        for index in range(6):
            WhatsAppMessage.objects.create(
                organization=self.organization,
                account=account,
                lead=self.lead,
                external_id=f"bounded-conversation-{index:02d}",
                direction=WhatsAppMessage.Direction.INBOUND,
                status=WhatsAppMessage.Status.RECEIVED,
                message_type=WhatsAppMessage.MessageType.TEXT,
                from_number="+919100000066",
                to_number="+919000000066",
                body=f"Conversation message {index:02d}",
            )
        for index in range(2):
            WhatsAppMessage.objects.create(
                organization=self.organization,
                account=account,
                lead=self.lead,
                external_id=f"bounded-error-{index:02d}",
                direction=WhatsAppMessage.Direction.OUTBOUND,
                status=WhatsAppMessage.Status.FAILED,
                message_type=WhatsAppMessage.MessageType.TEXT,
                from_number="+919000000066",
                to_number="+919100000066",
                body="Failed diagnostic message",
                error="provider failure",
            )
        for index in range(2):
            TriggerEvent.objects.create(
                organization=self.organization,
                lead=self.lead,
                kind="lead_updated",
                key=f"bounded-event-{index:02d}",
                payload={},
            )

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        found = self._result(
            self._call(
                bearer,
                "find_leads",
                {
                    "query": "Bounded Diagnostic Lead",
                    "limit": 10,
                },
            )
        )
        self.assertFalse(found["isError"])
        found_data = found["structuredContent"]
        self.assertEqual(found_data["match_count"], 11)
        self.assertEqual(found_data["matches_returned"], 10)
        self.assertTrue(found_data["matches_truncated"])

        conversation = self._result(
            self._call(
                bearer,
                "get_conversation",
                {
                    "lead_id": str(self.lead.id),
                    "channel": "whatsapp",
                    "limit": 5,
                },
            )
        )
        self.assertFalse(conversation["isError"])
        conversation_data = conversation["structuredContent"]
        self.assertGreaterEqual(conversation_data["message_count"], 8)
        self.assertEqual(conversation_data["messages_returned"], 5)
        self.assertTrue(conversation_data["messages_truncated"])
        self.assertEqual(
            conversation_data["channel_counts"]["instagram"],
            0,
        )

        workflow = self._result(
            self._call(
                bearer,
                "get_workflow_trace",
                {
                    "lead_id": str(self.lead.id),
                    "limit": 1,
                },
            )
        )
        self.assertFalse(workflow["isError"])
        workflow_data = workflow["structuredContent"]
        self.assertGreaterEqual(workflow_data["event_count"], 2)
        self.assertEqual(workflow_data["events_returned"], 1)
        self.assertTrue(workflow_data["events_truncated"])

        recent = self._result(
            self._call(
                bearer,
                "get_recent_errors",
                {
                    "hours": 24,
                    "limit": 1,
                },
            )
        )
        self.assertFalse(recent["isError"])
        whatsapp_counts = recent["structuredContent"][
            "result_counts"
        ]["whatsapp"]
        self.assertGreaterEqual(whatsapp_counts["total"], 2)
        self.assertEqual(whatsapp_counts["returned"], 1)
        self.assertTrue(whatsapp_counts["truncated"])

    def test_ai_diagnostics_are_read_only_and_never_decrypt_provider_credentials(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        OrgInfo.objects.filter(
            organization=self.organization
        ).delete()
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="AI Diagnostic Credential Safe Sender",
            display_phone_number="+919000000064",
            phone_number_id="ai-diagnostic-safe-sender",
            access_token="ai-diagnostic-provider-secret",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        inbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            external_id="ai-diagnostic-safe-inbound",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number=self.lead.phone,
            to_number="+919000000064",
            body="diagnostic question",
        )
        foreign_account = WhatsAppAccount.objects.create(
            organization=self.other_organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Foreign AI Diagnostic Sender",
            display_phone_number="+919000000065",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        foreign_linked = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=foreign_account,
            lead=self.lead,
            external_id="ai-diagnostic-corrupt-newer",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.FAILED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919000000065",
            to_number=self.lead.phone,
            body="foreign-linked diagnostic message",
            error="foreign diagnostic relation",
        )
        WhatsAppMessage.objects.filter(
            pk=foreign_linked.pk
        ).update(
            created_at=timezone.now()
            + timedelta(seconds=1)
        )

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        self.assertFalse(
            OrgInfo.objects.filter(
                organization=self.organization
            ).exists()
        )
        with patch(
            "apps.channels.models.EncryptedTextField.from_db_value",
            side_effect=AssertionError(
                "AI diagnostics must not decrypt provider credentials"
            ),
        ):
            result = self._result(
                self._call(
                    bearer,
                    "get_ai_diagnostics",
                    {"lead_id": str(self.lead.id)},
                )
            )

        self.assertFalse(result["isError"])
        self.assertFalse(
            OrgInfo.objects.filter(
                organization=self.organization
            ).exists()
        )
        diagnostic = result["structuredContent"]
        self.assertEqual(
            diagnostic["latest_message"]["id"],
            str(inbound.id),
        )
        self.assertEqual(
            diagnostic["account_id"],
            str(account.id),
        )
        payload = json.dumps(diagnostic)
        self.assertNotIn(
            "ai-diagnostic-provider-secret",
            payload,
        )
        self.assertNotIn(
            str(foreign_account.id),
            payload,
        )
        self.assertNotIn(
            "foreign diagnostic relation",
            payload,
        )

    def test_conversation_diagnostics_redact_customer_secret_patterns(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Conversation Privacy Sender",
            display_phone_number="+919000000060",
            phone_number_id="conversation-privacy-sender",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        opaque_secret = "Z" * 64
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            external_id="conversation-secret-message",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919100000060",
            to_number="+919000000060",
            body=(
                "Normal customer question. "
                "password: customer-password-secret "
                + opaque_secret
            ),
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
                "get_conversation",
                {
                    "lead_id": str(self.lead.id),
                    "channel": "whatsapp",
                    "limit": 10,
                },
            )
        )
        self.assertFalse(result["isError"])
        payload = json.dumps(result["structuredContent"])
        self.assertIn("Normal customer question.", payload)
        self.assertIn("password=[REDACTED]", payload)
        self.assertNotIn("customer-password-secret", payload)
        self.assertNotIn(opaque_secret, payload)

    def test_conversation_and_trace_diagnostics_never_decrypt_provider_credentials(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        wa_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Diagnostic Credential Safe WhatsApp",
            display_phone_number="+919000000063",
            phone_number_id="diagnostic-credential-safe-wa",
            access_token="diagnostic-whatsapp-provider-secret",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        wa_message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=wa_account,
            lead=self.lead,
            external_id="diagnostic-credential-safe-wa-message",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919100000063",
            to_number="+919000000063",
            body="WhatsApp diagnostic message",
        )

        ig_account = InstagramAccount.objects.create(
            organization=self.organization,
            ig_user_id="diagnostic-credential-safe-instagram",
            username="diagnostic_safe_instagram",
            access_token="diagnostic-instagram-provider-secret",
            status=InstagramAccount.Status.CONNECTED,
        )
        ig_conversation = InstagramConversation.objects.create(
            organization=self.organization,
            account=ig_account,
            lead=self.lead,
            participant_id="diagnostic-participant",
            participant_username="diagnostic_participant",
        )
        opaque_secret = "Y" * 64
        ig_message = InstagramMessage.objects.create(
            organization=self.organization,
            account=ig_account,
            conversation=ig_conversation,
            external_id="diagnostic-credential-safe-ig-message",
            direction=InstagramMessage.Direction.INBOUND,
            status=InstagramMessage.Status.RECEIVED,
            message_type=InstagramMessage.MessageType.TEXT,
            body=(
                "Instagram customer question with opaque token "
                + opaque_secret
            ),
            attachments=[
                {
                    "type": opaque_secret,
                    "url": "https://example.test/private-media",
                },
                *[
                    {
                        "type": f"media-{index:02d}",
                        "url": (
                            "https://example.test/private-media/"
                            f"{index:02d}"
                        ),
                    }
                    for index in range(10)
                ],
            ],
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        with patch(
            "apps.channels.models.EncryptedTextField.from_db_value",
            side_effect=AssertionError(
                "Operations diagnostics must not decrypt provider credentials"
            ),
        ):
            conversation = self._result(
                self._call(
                    bearer,
                    "get_conversation",
                    {
                        "lead_id": str(self.lead.id),
                        "channel": "all",
                        "limit": 10,
                    },
                )
            )
            self.assertFalse(conversation["isError"])
            instagram_row = next(
                item
                for item in conversation["structuredContent"][
                    "messages"
                ]
                if item["channel"] == "instagram"
            )
            self.assertEqual(
                instagram_row["attachment_count"],
                11,
            )
            self.assertEqual(
                instagram_row["attachment_types_returned"],
                10,
            )
            self.assertTrue(
                instagram_row["attachment_types_truncated"]
            )

            wa_trace = self._result(
                self._call(
                    bearer,
                    "trace_message",
                    {"message_id": str(wa_message.id)},
                )
            )
            self.assertFalse(wa_trace["isError"])

            ig_trace = self._result(
                self._call(
                    bearer,
                    "trace_message",
                    {"message_id": str(ig_message.id)},
                )
            )
            self.assertFalse(ig_trace["isError"])

        payload = json.dumps(
            {
                "conversation": conversation["structuredContent"],
                "wa_trace": wa_trace["structuredContent"],
                "ig_trace": ig_trace["structuredContent"],
            }
        )
        self.assertNotIn(opaque_secret, payload)
        self.assertNotIn(
            "diagnostic-whatsapp-provider-secret",
            payload,
        )
        self.assertNotIn(
            "diagnostic-instagram-provider-secret",
            payload,
        )
        self.assertIn("[REDACTED]", payload)

    def test_recent_errors_bounds_hosted_provider_details(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Recent Error Hosted",
            display_phone_number="+919000000061",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        inbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            external_id="recent-error-hosted-source",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919100000061",
            to_number="+919000000061",
            body="hello",
        )
        job = HostedAutomationJob.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            source_message=inbound,
            available_at=timezone.now(),
            status=HostedAutomationJob.Status.FAILED,
            result={
                "reason": "pipeline_whatsapp_account_mismatch",
                "delivery": {"status": "blocked"},
                "internal_detail": "private hosted result",
            },
            error=(
                "access_token=recent-hosted-provider-secret "
                "customer body should not be returned"
            ),
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
                "get_recent_errors",
                {"hours": 24, "limit": 20},
            )
        )
        self.assertFalse(result["isError"])
        hosted_counts = result["structuredContent"][
            "result_counts"
        ]["hosted"]
        self.assertGreaterEqual(hosted_counts["total"], 1)
        self.assertEqual(
            hosted_counts["returned"],
            len(result["structuredContent"]["hosted"]),
        )
        self.assertFalse(hosted_counts["truncated"])
        hosted_rows = result["structuredContent"]["hosted"]
        row = next(
            item
            for item in hosted_rows
            if item["job_id"] == str(job.id)
        )
        self.assertEqual(
            row["reason"],
            "pipeline_whatsapp_account_mismatch",
        )
        self.assertEqual(
            row["delivery_status"],
            "blocked",
        )
        self.assertTrue(row["has_persisted_error"])
        payload = json.dumps(result["structuredContent"])
        self.assertNotIn(
            "recent-hosted-provider-secret",
            payload,
        )
        self.assertNotIn(
            "customer body should not be returned",
            payload,
        )
        self.assertNotIn(
            "private hosted result",
            payload,
        )
        self.assertNotIn("internal_detail", payload)

    def test_trace_message_bounds_hosted_job_internal_details(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Trace",
            display_phone_number="+919000000011",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        inbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            external_id="trace-hosted-sensitive",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919100000000",
            to_number="+919000000011",
            body="hello",
            raw_payload={
                "shvya_ai_execution": {
                    "status": "failed",
                    "reason": "delivery_blocked",
                    "attempts": 2,
                    "updated_at": "2026-09-21T10:00:00+00:00",
                    "provider_prompt": "private execution prompt",
                    "provider_response": "private provider response",
                },
                "shvya_ai_processing": {
                    "processed": False,
                    "message_id": "safe-processing-link",
                    "internal_prompt": "private processing prompt",
                    "qualification_response_plan": {
                        "prompt": "private qualification plan",
                    },
                },
                "shvya_ai": {
                    "source_inbound_message_id": "safe-source-link",
                    "origin": "ai",
                    "model": "private-model-name",
                    "prompt": "private outbound prompt",
                },
            },
        )
        HostedAutomationJob.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            source_message=inbound,
            available_at=timezone.now(),
            status=HostedAutomationJob.Status.FAILED,
            result={
                "reason": "lead_ai_disabled",
                "delivery": {"status": "blocked"},
                "internal_prompt": "private system prompt",
                "free_text": "password: hosted-result-secret",
            },
            error="access_token=hosted-provider-secret",
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
                "trace_message",
                {"message_id": str(inbound.id)},
            )
        )
        self.assertFalse(result["isError"])
        markers = result["structuredContent"]["ai_markers"]
        self.assertEqual(
            markers["execution"]["status"],
            "failed",
        )
        self.assertEqual(
            markers["execution"]["reason"],
            "delivery_blocked",
        )
        self.assertEqual(
            markers["processing"]["processed"],
            False,
        )
        self.assertEqual(
            markers["outbound_linkage"]["origin"],
            "ai",
        )

        hosted = result["structuredContent"]["hosted_job"]
        self.assertEqual(hosted["status"], HostedAutomationJob.Status.FAILED)
        self.assertEqual(hosted["reason"], "lead_ai_disabled")
        self.assertEqual(hosted["delivery_status"], "blocked")
        self.assertTrue(hosted["has_persisted_error"])
        payload = json.dumps(result["structuredContent"])
        self.assertNotIn("private system prompt", payload)
        self.assertNotIn("hosted-result-secret", payload)
        self.assertNotIn("hosted-provider-secret", payload)
        self.assertNotIn("internal_prompt", payload)
        self.assertNotIn("free_text", payload)

    def test_instagram_webhook_failures_are_tenant_scoped_and_payload_safe(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        own_account = InstagramAccount.objects.create(
            organization=self.organization,
            ig_user_id="own-account",
            username="own_account",
            status=InstagramAccount.Status.CONNECTED,
        )
        other_account = InstagramAccount.objects.create(
            organization=self.other_organization,
            ig_user_id="other-account",
            username="other_account",
            status=InstagramAccount.Status.CONNECTED,
        )
        own = InstagramWebhookDelivery.objects.create(
            payload_sha256="1" * 64,
            raw_payload={
                "object": "instagram",
                "entry": [
                    {
                        "id": own_account.ig_user_id,
                        "private": "raw-own-webhook-secret",
                    }
                ],
            },
            status=InstagramWebhookDelivery.Status.FAILED,
            error_message="access_token=own-webhook-secret provider failure",
            processed_at=timezone.now(),
        )
        other = InstagramWebhookDelivery.objects.create(
            payload_sha256="2" * 64,
            raw_payload={
                "object": "instagram",
                "entry": [
                    {
                        "id": other_account.ig_user_id,
                        "private": "raw-other-webhook-secret",
                    }
                ],
            },
            status=InstagramWebhookDelivery.Status.FAILED,
            error_message="other tenant webhook failure",
            processed_at=timezone.now(),
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        recent = self._result(
            self._call(
                bearer,
                "get_recent_errors",
                {"hours": 24, "limit": 20},
            )
        )
        self.assertFalse(recent["isError"])
        rows = recent["structuredContent"]["instagram_webhooks"]
        self.assertEqual(
            [row["delivery_id"] for row in rows],
            [str(own.id)],
        )
        self.assertEqual(
            rows[0]["error"],
            "access_token=[REDACTED] provider failure",
        )
        payload = json.dumps(recent["structuredContent"])
        self.assertNotIn(str(other.id), payload)
        self.assertNotIn("raw-own-webhook-secret", payload)
        self.assertNotIn("raw-other-webhook-secret", payload)
        self.assertNotIn("own-webhook-secret", payload)

        runtime = self._result(
            self._call(
                bearer,
                "get_runtime_health",
                {},
            )
        )
        self.assertFalse(runtime["isError"])
        self.assertEqual(
            runtime["structuredContent"]["counts"][
                "instagram_webhook_failed_24h"
            ],
            1,
        )
        self.assertFalse(
            runtime["structuredContent"]["capabilities"][
                "instagram_ai_auto_reply_runtime"
            ]
        )

    def test_sanitizer_preserves_status_metadata_but_redacts_secret_values(self):
        cleaned = sanitize_data(
            {
                "credential_present": True,
                "credential_expired": False,
                "token_expires_at": "2026-09-22T10:00:00+00:00",
                "token_refreshed_at": "2026-09-20T10:00:00+00:00",
                "has_credential": True,
                "access_token": "actual-access-token-secret",
                "refresh_token": "actual-refresh-token-secret",
                "credential": "actual-credential-secret",
                "password": "actual-password-secret",
            }
        )
        self.assertIs(cleaned["credential_present"], True)
        self.assertIs(cleaned["credential_expired"], False)
        self.assertEqual(
            cleaned["token_expires_at"],
            "2026-09-22T10:00:00+00:00",
        )
        self.assertEqual(
            cleaned["token_refreshed_at"],
            "2026-09-20T10:00:00+00:00",
        )
        self.assertIs(cleaned["has_credential"], True)
        for key in (
            "access_token",
            "refresh_token",
            "credential",
            "password",
        ):
            self.assertEqual(cleaned[key], "[REDACTED]")

    def test_integration_health_never_decrypts_provider_credentials(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Health WA",
            phone_number_id="phone-health",
            display_phone_number="+919000000010",
            access_token="wa-health-secret",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        instagram = InstagramAccount.objects.create(
            organization=self.organization,
            ig_user_id="ig-health-user",
            username="health_account",
            access_token="ig-health-secret",
            status=InstagramAccount.Status.CONNECTED,
            webhook_subscribed=True,
            token_expires_at=timezone.now() - timedelta(minutes=5),
            token_refreshed_at=timezone.now() - timedelta(days=60),
            last_error="access_token=ig-last-error-secret expired",
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        with patch(
            "apps.channels.models.EncryptedTextField.from_db_value",
            side_effect=AssertionError(
                "diagnostic health must not decrypt provider credentials"
            ),
        ):
            result = self._result(
                self._call(
                    bearer,
                    "get_integration_health",
                    {},
                )
            )

        self.assertFalse(result["isError"])
        health = result["structuredContent"]
        wa = next(
            item
            for item in health["whatsapp"]
            if item["business_name"] == "Health WA"
        )
        self.assertTrue(wa["provider_auth_configured"])
        self.assertEqual(health["whatsapp_count"], 1)
        self.assertEqual(health["whatsapp_returned"], 1)
        self.assertFalse(health["whatsapp_truncated"])
        self.assertTrue(health["instagram"]["provider_auth_configured"])
        self.assertTrue(health["instagram"]["provider_auth_expired"])
        self.assertFalse(
            health["instagram"]["automation_capabilities"][
                "ai_auto_reply_runtime"
            ]
        )
        self.assertEqual(
            health["instagram"]["last_error"],
            "access_token=[REDACTED] expired",
        )
        self.assertEqual(
            health["instagram"]["provider_auth_expires_at"],
            instagram.token_expires_at.isoformat(),
        )
        payload = json.dumps(health)
        self.assertNotIn("wa-health-secret", payload)
        self.assertNotIn("ig-health-secret", payload)

    def test_integration_health_reports_whatsapp_account_truncation(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        WhatsAppAccount.objects.bulk_create(
            [
                WhatsAppAccount(
                    organization=self.organization,
                    connection_type=(
                        WhatsAppAccount.ConnectionType.API
                    ),
                    business_name=f"Bounded Health {index:03d}",
                    display_phone_number=(
                        f"+9188{index:010d}"
                    ),
                    status=WhatsAppAccount.Status.CONNECTED,
                    is_active=True,
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
                "get_integration_health",
                {},
            )
        )
        self.assertFalse(result["isError"])
        health = result["structuredContent"]
        self.assertEqual(health["whatsapp_count"], 101)
        self.assertEqual(health["whatsapp_returned"], 100)
        self.assertEqual(len(health["whatsapp"]), 100)
        self.assertTrue(health["whatsapp_truncated"])

    def test_full_ai_configuration_read_avoids_truncated_playbook_rewrite_risk(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        long_playbook = (
            "General sales guidance. " * 1800
        ) + " password: legacy-secret-value"
        info = OrgInfo.objects.create(
            organization=self.organization,
            about="Full business profile",
            bot_languages="English",
            ai_playbook=long_playbook,
        )
        self.assertGreater(len(long_playbook), 30000)

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        summary = self._result(
            self._call(
                bearer,
                "get_organization_configuration",
                {},
            )
        )
        self.assertFalse(summary["isError"])
        self.assertTrue(
            summary["structuredContent"]["ai"][
                "playbook_truncated"
            ]
        )
        self.assertEqual(
            summary["structuredContent"]["ai"][
                "playbook_length"
            ],
            len(long_playbook),
        )
        self.assertEqual(
            len(
                summary["structuredContent"]["ai"][
                    "playbook"
                ]
            ),
            30000,
        )

        full = self._result(
            self._call(
                bearer,
                "get_ai_configuration",
                {},
            )
        )
        self.assertFalse(full["isError"])
        data = full["structuredContent"]
        self.assertTrue(data["configured"])
        self.assertEqual(
            data["ai_playbook_length"],
            len(long_playbook),
        )
        self.assertTrue(
            data["redactions"]["ai_playbook"]
        )
        self.assertIn(
            "legacy-secret-value",
            long_playbook,
        )
        self.assertNotIn(
            "legacy-secret-value",
            data["ai_playbook"],
        )
        self.assertIn(
            "password=[REDACTED]",
            data["ai_playbook"],
        )
        self.assertEqual(
            data["about"],
            info.about,
        )

    def test_full_ai_configuration_flags_legacy_text_over_canonical_limit(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        legacy_playbook = ("Legacy guidance. " * 7000)
        self.assertGreater(len(legacy_playbook), 100000)
        OrgInfo.objects.create(
            organization=self.organization,
            ai_playbook=legacy_playbook,
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
                "get_ai_configuration",
                {},
            )
        )
        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        self.assertEqual(
            data["ai_playbook_length"],
            len(legacy_playbook),
        )
        self.assertTrue(data["ai_playbook_truncated"])
        self.assertEqual(len(data["ai_playbook"]), 100000)

    def test_knowledge_health_is_bounded_metadata_only_and_tenant_scoped(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        source = KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="https://example.com/pricing?token=source-secret",
            url="https://example.com/pricing?token=source-secret",
            is_active=True,
        )
        document = Document.objects.create(
            organization=self.organization,
            name="https://example.com/pricing?token=document-secret",
            source_key="https://example.com/pricing?token=internal-source-key",
            version=2,
            source_url="https://example.com/pricing?token=document-secret",
            file="ai_knowledge/private-pricing-secret.pdf",
            share_instruction="private share instruction",
            processing_status=Document.ProcessingStatus.COMPLETED,
            processing_error="",
            is_active=True,
        )
        Chunk.objects.create(
            document=document,
            organization=self.organization,
            content="private knowledge body that must never be returned",
            chunk_index=0,
            embedding=[0.0] * 1536,
            is_active=True,
        )
        opaque_label_secret = "A" * 64
        failed = Document.objects.create(
            organization=self.organization,
            name=f"Failed knowledge {opaque_label_secret}",
            source_key="failed-source",
            version=1,
            processing_status=Document.ProcessingStatus.FAILED,
            processing_error="password: ingestion-secret",
            is_active=False,
        )

        foreign = Document.objects.create(
            organization=self.other_organization,
            name="Foreign knowledge",
            source_key="foreign-source",
            version=1,
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        Chunk.objects.create(
            document=foreign,
            organization=self.other_organization,
            content="foreign tenant knowledge body",
            chunk_index=0,
            is_active=True,
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
                "get_knowledge_health",
                {"limit": 50},
            )
        )
        self.assertFalse(result["isError"])
        health = result["structuredContent"]
        self.assertEqual(health["summary"]["source_count"], 1)
        self.assertEqual(health["summary"]["document_count"], 2)
        self.assertEqual(
            health["summary"]["active_completed_documents"],
            1,
        )
        self.assertEqual(health["summary"]["failed_documents"], 1)
        self.assertEqual(health["summary"]["active_chunks"], 1)
        self.assertEqual(
            health["summary"]["embedded_active_chunks"],
            1,
        )
        self.assertEqual(
            health["summary"]["embedding_coverage"],
            1.0,
        )

        self.assertEqual(
            health["sources"][0]["id"],
            str(source.id),
        )
        self.assertEqual(
            health["sources"][0]["name"],
            "example.com",
        )
        self.assertEqual(
            health["sources"][0]["url_host"],
            "example.com",
        )
        active_doc = next(
            item
            for item in health["documents"]
            if item["id"] == str(document.id)
        )
        self.assertEqual(active_doc["name"], "example.com")
        self.assertEqual(
            active_doc["source_url_host"],
            "example.com",
        )
        self.assertTrue(active_doc["has_file"])
        self.assertTrue(
            active_doc["share_instruction_present"]
        )
        failed_doc = next(
            item
            for item in health["documents"]
            if item["id"] == str(failed.id)
        )
        self.assertTrue(failed_doc["has_processing_error"])

        payload = json.dumps(health)
        self.assertNotIn("source-secret", payload)
        self.assertNotIn("document-secret", payload)
        self.assertNotIn("internal-source-key", payload)
        self.assertNotIn("private knowledge body", payload)
        self.assertNotIn("private-pricing-secret", payload)
        self.assertNotIn("private share instruction", payload)
        self.assertNotIn("ingestion-secret", payload)
        self.assertNotIn(opaque_label_secret, payload)
        self.assertNotIn("foreign tenant knowledge body", payload)
        self.assertNotIn(
            str(foreign.id),
            {item["id"] for item in health["documents"]},
        )
        self.assertNotIn("embedding", payload.lower().replace("embedding_coverage", "").replace("embedded_chunk_count", "").replace("embedded_active_chunks", ""))

    def test_lead_snapshot_redacts_sensitive_crm_attribute_values(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        safe_definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Company Size Snapshot",
            key="company_size_snapshot",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        sensitive_definition = AttributeDefinition.objects.create(
            organization=self.organization,
            name="API Key",
            key="integration_reference",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead.attributes = {
            **(self.lead.attributes or {}),
            safe_definition.key: "25",
            sensitive_definition.key: "short-sensitive-value",
            "legacy_orphan_context": "legacy-orphan-secret-value",
        }
        self.lead.save(
            update_fields=["attributes", "updated_at"]
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        snapshot = self._result(
            self._call(
                bearer,
                "get_lead_snapshot",
                {"lead_id": str(self.lead.id)},
            )
        )
        self.assertFalse(snapshot["isError"])
        data = snapshot["structuredContent"]
        self.assertEqual(
            data["attributes"][safe_definition.key],
            "25",
        )
        self.assertNotIn(
            sensitive_definition.key,
            data["attributes"],
        )
        self.assertEqual(
            data["sensitive_attributes_redacted"],
            1,
        )
        self.assertEqual(
            data["undefined_attributes_omitted"],
            1,
        )
        self.assertNotIn(
            "legacy_orphan_context",
            data["attributes"],
        )
        payload = json.dumps(data)
        self.assertNotIn(
            "short-sensitive-value",
            payload,
        )
        self.assertNotIn(
            "legacy-orphan-secret-value",
            payload,
        )

    def test_operations_reads_redact_sensitive_attributes_workflows_and_playbook_secrets(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        sensitive = AttributeDefinition.objects.create(
            organization=self.organization,
            name="API Key",
            key="api_key",
            field_type=AttributeDefinition.FieldType.TEXT,
            description="Credential-like field that must stay private.",
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_playbook = (
            "Use this safe business rule. password: secret-pass-value "
            "Never disclose it."
        )
        info.save(update_fields=["ai_playbook"])

        SmartTrigger.objects.create(
            organization=self.organization,
            name="Sensitive legacy workflow",
            enabled=True,
            position=1,
            trigger_type="keyword",
            conditions={
                "scopes": [],
                "attributes": [
                    {
                        "key": sensitive.key,
                        "match": "equals",
                        "values": ["workflow-secret-value"],
                    }
                ],
            },
            action_type="attribute",
            action={
                "key": sensitive.key,
                "value": "workflow-action-secret",
            },
            fingerprint="f" * 64,
            created_by=self.admin,
        )

        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        config = self._result(
            self._call(bearer, "get_organization_configuration")
        )
        self.assertFalse(config["isError"])
        config_json = json.dumps(config["structuredContent"])
        self.assertNotIn('"api_key"', config_json)
        self.assertNotIn("secret-pass-value", config_json)
        self.assertIn("[REDACTED]", config_json)

        automation = self._result(
            self._call(bearer, "get_automation_configuration")
        )
        self.assertFalse(automation["isError"])
        automation_json = json.dumps(automation["structuredContent"])
        self.assertNotIn("workflow-secret-value", automation_json)
        self.assertNotIn("workflow-action-secret", automation_json)
        self.assertNotIn('"api_key"', automation_json)
        self.assertIn("[REDACTED_SENSITIVE_ATTRIBUTE]", automation_json)
        self.assertGreaterEqual(
            automation["structuredContent"]["counts"][
                "sensitive_workflow_fields_redacted"
            ],
            2,
        )

    def test_conversion_analysis_keeps_source_shift_causal_language_bounded(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        previous_created_at = (
            timezone.now() - timedelta(days=45)
        )
        for index in range(5):
            lead = Lead.objects.create(
                organization=self.organization,
                pipeline=self.pipeline,
                stage=self.new_stage,
                name=f"Previous Meta {index}",
                phone=f"+9198100000{index:02d}",
                lead_source="meta_ads",
            )
            Lead.objects.filter(pk=lead.pk).update(
                created_at=previous_created_at,
            )

        for index in range(5):
            Lead.objects.create(
                organization=self.organization,
                pipeline=self.pipeline,
                stage=self.new_stage,
                name=f"Current WhatsApp {index}",
                phone=f"+9198200000{index:02d}",
                lead_source="whatsapp",
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
                "get_conversion_analysis",
                {"days": 30},
            )
        )
        self.assertFalse(result["isError"])
        analysis = result["structuredContent"]
        observations = analysis["observations"]

        self.assertTrue(
            any(
                item["classification"] == "Measured"
                and item["metric"] == "source_mix_share"
                for item in observations
            )
        )
        self.assertTrue(
            any(
                item["classification"] == "Hypothesis"
                and item["metric"] == "source_mix"
                for item in observations
            )
        )
        self.assertFalse(
            any(
                item["classification"] == "Confirmed cause"
                for item in observations
            )
        )

        current_sources = {
            row["source"]: row
            for row in analysis["comparison"][
                "current_period"
            ]["source_mix"]
        }
        previous_sources = {
            row["source"]: row
            for row in analysis["comparison"][
                "previous_period"
            ]["source_mix"]
        }
        self.assertGreater(
            current_sources["whatsapp"]["lead_share"],
            previous_sources.get(
                "whatsapp",
                {"lead_share": 0},
            )["lead_share"],
        )
        self.assertGreater(
            previous_sources["meta_ads"]["lead_share"],
            current_sources.get(
                "meta_ads",
                {"lead_share": 0},
            )["lead_share"],
        )

    def test_conversion_analysis_ignores_corrupt_tenant_relations(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        corrupt_lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.other_lead.pipeline,
            stage=self.other_lead.stage,
            name="Corrupt Conversion Lead",
            phone="+919999999994",
            lead_source="meta_ads",
        )
        own_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Conversion Safe Sender",
            display_phone_number="+919000000070",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        foreign_account = WhatsAppAccount.objects.create(
            organization=self.other_organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Foreign Conversion Sender",
            display_phone_number="+919000000071",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=own_account,
            lead=self.lead,
            external_id="conversion-valid-outbound",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.SENT,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919000000070",
            to_number=self.lead.phone,
            body="valid outbound",
        )
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=foreign_account,
            lead=self.lead,
            external_id="conversion-corrupt-outbound",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.FAILED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919000000071",
            to_number=self.lead.phone,
            body="corrupt outbound",
        )
        source = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=own_account,
            lead=self.lead,
            external_id="conversion-hosted-source",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number=self.lead.phone,
            to_number="+919000000070",
            body="source",
        )
        HostedAutomationJob.objects.create(
            organization=self.organization,
            account=foreign_account,
            lead=self.lead,
            source_message=source,
            available_at=timezone.now(),
            status=HostedAutomationJob.Status.FAILED,
            result={"reason": "foreign_account_relation"},
            error="corrupt hosted failure",
        )

        workflow = SmartTrigger.objects.create(
            organization=self.organization,
            name="Conversion Relation Guard Workflow",
            enabled=True,
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
            fingerprint="c" * 64,
            created_by=self.admin,
        )
        foreign_event = TriggerEvent.objects.create(
            organization=self.other_organization,
            lead=self.other_lead,
            kind="lead_updated",
            key="foreign-conversion-event",
            payload={},
        )
        TriggerRun.objects.create(
            rule=workflow,
            event=foreign_event,
            lead=self.lead,
            action_type="ai",
            action={"enabled": True},
            status="failed",
            detail="corrupt conversion workflow failure",
            due_at=timezone.now(),
            finished_at=timezone.now(),
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
                "get_conversion_analysis",
                {"days": 30},
            )
        )
        self.assertFalse(result["isError"])
        current = result["structuredContent"]["comparison"][
            "current_period"
        ]
        self.assertEqual(current["lead_volume"], 1)
        self.assertEqual(
            current["messaging"]["outbound_total"],
            1,
        )
        self.assertEqual(
            current["messaging"]["successful_outbound"],
            1,
        )
        self.assertEqual(
            current["messaging"]["failed_outbound"],
            0,
        )
        self.assertEqual(
            current["failures"]["workflow_failures"],
            0,
        )
        self.assertEqual(
            current["failures"]["hosted_ai_failures"],
            0,
        )
        source_names = {
            row["source"]
            for row in current["source_mix"]
        }
        self.assertNotIn("meta_ads", source_names)
        payload = json.dumps(result["structuredContent"])
        self.assertNotIn(str(corrupt_lead.id), payload)
        self.assertNotIn(
            self.other_organization.name,
            payload,
        )
        self.assertNotIn(
            self.other_lead.pipeline.name,
            payload,
        )

    def test_conversion_analysis_uses_same_created_lead_cohort_for_rate(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        old_lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Old Lead",
            phone="+919999999993",
        )
        old_created_at = timezone.now() - timedelta(days=45)
        Lead.objects.filter(pk=old_lead.pk).update(
            created_at=old_created_at,
        )
        old_lead.refresh_from_db()
        move_lead_to_stage(
            lead=old_lead,
            stage=self.qualified,
            actor=self.admin,
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
                "get_conversion_analysis",
                {"days": 30},
            )
        )
        self.assertFalse(result["isError"])
        current = result["structuredContent"]["comparison"][
            "current_period"
        ]
        self.assertEqual(current["lead_volume"], 1)
        self.assertEqual(current["qualified_transitions"], 0)
        self.assertEqual(
            current["qualified_transition_rate"],
            0.0,
        )

    def test_find_affected_leads_finds_same_issue_without_cross_tenant_leakage(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        self.lead.attributes = {
            "_shvya_ai_qualification": {
                "qualification_status": "completed",
            }
        }
        self.lead.save(
            update_fields=[
                "attributes",
                "updated_at",
            ]
        )
        self.other_lead.attributes = {
            "_shvya_ai_qualification": {
                "qualification_status": "completed",
            }
        }
        self.other_lead.save(
            update_fields=[
                "attributes",
                "updated_at",
            ]
        )
        bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )

        tool_names = {
            item["name"]
            for item in self._list_tools(
                bearer
            )["tools"]
        }
        self.assertIn(
            "find_affected_leads",
            tool_names,
        )

        result = self._result(
            self._call(
                bearer,
                "find_affected_leads",
                {
                    "issue_type": (
                        "qualification_completed_not_qualified"
                    ),
                    "limit": 20,
                },
            )
        )
        self.assertFalse(result["isError"])
        affected = result["structuredContent"]
        self.assertEqual(
            affected["match_count"],
            1,
        )
        self.assertEqual(
            affected["matches_returned"],
            1,
        )
        self.assertFalse(
            affected["matches_truncated"]
        )
        self.assertEqual(
            affected["matches"][0]["id"],
            str(self.lead.id),
        )
        payload = json.dumps(affected)
        self.assertNotIn(
            str(self.other_lead.id),
            payload,
        )
        self.assertNotIn(
            self.other_organization.name,
            payload,
        )

    def test_conversion_analysis_reports_first_response_ageing_and_lost_reasons(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        AnalyticsSettings.objects.create(
            organization=self.organization,
            hot_lead_stage=self.qualified,
            lead_won_stage=self.qualified,
            lead_lost_stage=self.review_stage,
            stall_day_threshold=3,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Lost Reason",
            key="lost_reason",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        now = timezone.now()
        Lead.objects.filter(
            pk=self.lead.pk
        ).update(
            attributes={
                "lost_reason": "Budget",
                "_shvya_ai_qualification": {
                    "qualification_status": (
                        "completed"
                    ),
                },
            },
            stage_entered_at=(
                now - timedelta(days=5)
            ),
        )
        self.lead.refresh_from_db()

        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=(
                WhatsAppAccount.ConnectionType.API
            ),
            business_name="Conversion Metrics Sender",
            display_phone_number="+919000000081",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        inbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            external_id="conversion-first-response-inbound",
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number=self.lead.phone,
            to_number="+919000000081",
            body="Hello",
        )
        outbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=account,
            lead=self.lead,
            external_id="conversion-first-response-outbound",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.SENT,
            message_type=WhatsAppMessage.MessageType.TEXT,
            from_number="+919000000081",
            to_number=self.lead.phone,
            body="Hello, how can I help?",
        )
        WhatsAppMessage.objects.filter(
            pk=inbound.pk
        ).update(
            created_at=now - timedelta(minutes=10)
        )
        WhatsAppMessage.objects.filter(
            pk=outbound.pk
        ).update(
            created_at=now - timedelta(minutes=5)
        )
        LeadActivity.objects.create(
            organization=self.organization,
            lead=self.lead,
            topic=LeadActivity.Topic.STAGE_CHANGED,
            actor=self.admin,
            actor_name=self.admin.name,
            old_pipeline=self.pipeline,
            old_pipeline_name=self.pipeline.name,
            new_pipeline=self.pipeline,
            new_pipeline_name=self.pipeline.name,
            old_stage=self.new_stage,
            old_stage_name=self.new_stage.name,
            new_stage=self.review_stage,
            new_stage_name=self.review_stage.name,
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
                "get_conversion_analysis",
                {"days": 30},
            )
        )
        self.assertFalse(result["isError"])
        analysis = result["structuredContent"]
        current = analysis["comparison"][
            "current_period"
        ]
        whatsapp_response = current[
            "messaging"
        ]["first_response"]["whatsapp"]
        self.assertEqual(
            whatsapp_response["inbound_leads"],
            1,
        )
        self.assertEqual(
            whatsapp_response["responded_leads"],
            1,
        )
        self.assertEqual(
            whatsapp_response["response_rate"],
            1.0,
        )
        self.assertGreaterEqual(
            whatsapp_response[
                "average_first_response_seconds"
            ],
            250,
        )
        self.assertLessEqual(
            whatsapp_response[
                "average_first_response_seconds"
            ],
            350,
        )

        self.assertEqual(
            current["qualification_completion"][
                "completed_leads"
            ],
            1,
        )
        self.assertEqual(
            current["qualification_completion"][
                "completion_rate"
            ],
            1.0,
        )
        stage_reach = current[
            "stage_conversion_proxy"
        ]["stages"]
        self.assertTrue(
            any(
                row["stage_id"]
                == str(self.review_stage.id)
                and row["lead_count"] == 1
                for row in stage_reach
            )
        )

        self.assertEqual(
            current["lost"]["lost_transitions"],
            1,
        )
        self.assertTrue(
            current["lost"][
                "reason_capture_configured"
            ]
        )
        self.assertIn(
            {
                "reason": "Budget",
                "count": 1,
            },
            current["lost"]["reason_counts"],
        )

        ageing = analysis[
            "current_snapshot"
        ]["lead_ageing"]
        self.assertEqual(
            ageing["stall_day_threshold"],
            3,
        )
        self.assertEqual(
            ageing["stalled_count"],
            1,
        )
        self.assertGreaterEqual(
            ageing["buckets"]["days_3_to_7"],
            1,
        )

    def test_instagram_health_reports_full_diagnostic_chain_without_secrets(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
            ],
        )
        instagram = InstagramAccount.objects.create(
            organization=self.organization,
            ig_user_id="ig-complete-health-user",
            username="complete_health",
            access_token="ig-complete-health-secret",
            status=InstagramAccount.Status.CONNECTED,
            webhook_subscribed=True,
            subscribed_fields=[
                "messages",
                "messaging_postbacks",
            ],
            token_expires_at=(
                timezone.now()
                + timedelta(days=30)
            ),
            last_webhook_at=timezone.now(),
        )
        conversation = (
            InstagramConversation.objects.create(
                organization=self.organization,
                account=instagram,
                lead=self.lead,
                participant_id="ig-participant-1",
                participant_username="lead_user",
            )
        )
        InstagramMessage.objects.create(
            organization=self.organization,
            account=instagram,
            conversation=conversation,
            external_id="ig-complete-health-message",
            direction=InstagramMessage.Direction.INBOUND,
            status=InstagramMessage.Status.RECEIVED,
            message_type=InstagramMessage.MessageType.SHARE,
            sender_id="ig-participant-1",
            recipient_id=instagram.ig_user_id,
            body="Shared a reel",
            attachments=[
                {
                    "type": "ig_reel",
                    "payload": {
                        "link": (
                            "https://www.instagram.com/"
                            "reel/example/"
                        ),
                    },
                }
            ],
            sent_at=timezone.now(),
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
                "get_integration_health",
                {},
            )
        )
        self.assertFalse(result["isError"])
        health = result["structuredContent"][
            "instagram"
        ]
        chain = health["diagnostic_chain"]
        self.assertTrue(
            chain["connection"]["connected"]
        )
        self.assertTrue(
            chain["webhook"]["subscribed"]
        )
        self.assertFalse(
            chain["permissions"][
                "grant_scope_list_persisted"
            ]
        )
        self.assertEqual(
            chain["permissions"][
                "verification_status"
            ],
            "unavailable_from_persisted_state",
        )
        self.assertEqual(
            chain["message_event"]["inbound_24h"],
            1,
        )
        self.assertEqual(
            chain["media_story_reel_handling"][
                "type_counts"
            ]["reel"],
            1,
        )
        self.assertEqual(
            chain["lead_creation_and_linking"][
                "linked_to_lead"
            ],
            1,
        )
        self.assertEqual(
            chain["pipeline_mapping"][
                "pipelines"
            ][0]["pipeline_id"],
            str(self.pipeline.id),
        )
        self.assertEqual(
            chain["outbound_eligibility"][
                "standard_window_conversations"
            ],
            1,
        )
        self.assertFalse(
            chain["ai_processing"][
                "runtime_available"
            ]
        )
        self.assertEqual(
            chain[
                "first_known_ai_auto_reply_blocker"
            ],
            "ai_processing_runtime",
        )
        self.assertNotIn(
            "ig-complete-health-secret",
            json.dumps(health),
        )
