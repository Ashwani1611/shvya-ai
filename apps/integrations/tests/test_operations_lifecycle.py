import json
from datetime import timedelta

from django.db.models import Max
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_engagement.models import FAQ
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.followups.models import FollowupSequence, LeadSequenceState
from apps.followups.touchpoint_models import TouchpointCategory, TouchpointReply
from apps.integrations.models import (
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsPolicy,
)
from apps.integrations.operations_auth import (
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    token_hash,
)
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_CONFIGURATION_PLAN_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
    ROLE_ORGANIZATION_ADMIN,
    capabilities_for_grant,
)
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerEvent, TriggerRun


class OperationsLifecycleToolsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Lifecycle Org")
        self.admin = User.objects.create_user(
            email="lifecycle-admin@example.test",
            organization=self.organization,
            password=None,
            name="Lifecycle Admin",
            role=User.Role.ADMIN,
        )
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
                CAP_AI_CONFIG_WRITE,
                CAP_PIPELINE_CONFIG_WRITE,
                CAP_STAGE_CONFIG_WRITE,
                CAP_ATTRIBUTE_CONFIG_WRITE,
                CAP_WORKFLOW_CONFIG_WRITE,
                CAP_CADENCE_CONFIG_WRITE,
                CAP_MESSAGING_CONFIG_WRITE,
                CAP_CONFIGURATION_PLAN_WRITE,
            ],
            approval_required_capabilities=[],
            updated_by=self.admin,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Lifecycle Sales",
        )
        self.new_stage = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Lifecycle Lead",
            phone="+919800000001",
        )
        self.oauth_client = OperationsOAuthClient.objects.create(
            client_id="lifecycle_mcp_test_client",
            client_name="Lifecycle MCP Test",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        raw = "lifecycle-bearer"
        scopes = [OPERATIONS_READ_SCOPE, OPERATIONS_WRITE_SCOPE]
        OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=self.admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash(raw),
            refresh_token_hash=token_hash("lifecycle-refresh"),
            scope=" ".join(scopes),
            granted_capabilities=sorted(
                capabilities_for_grant(
                    role=ROLE_ORGANIZATION_ADMIN,
                    organization=self.organization,
                    allow_writes=True,
                )
            ),
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=4),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        self.bearer = raw

    def _call(self, name, arguments=None):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.bearer,
            HTTP_X_REAL_IP="198.51.100.88",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def _ok(self, name, arguments=None):
        result = self._call(name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _custom_stage(self, pipeline=None, name="Review"):
        pipeline = pipeline or self.pipeline
        order = (
            pipeline.stages.aggregate(value=Max("display_order"))["value"] or 0
        ) + 1
        return Stage.objects.create(
            pipeline=pipeline,
            name=name,
            description="Temporary lifecycle stage.",
            display_order=order,
            is_active=True,
            ai_on=True,
        )

    def test_lifecycle_tools_are_discoverable_and_deletes_are_destructive(self):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "tools",
                    "method": "tools/list",
                    "params": {},
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.bearer,
            HTTP_X_REAL_IP="198.51.100.88",
        )
        self.assertEqual(response.status_code, 200)
        tools = {item["name"]: item for item in response.json()["result"]["tools"]}
        expected = {
            "archive_stage",
            "delete_stage",
            "archive_attribute",
            "delete_attribute",
            "archive_pipeline",
            "archive_cadence",
            "archive_workflow",
            "archive_touchpoint",
            "archive_faq",
        }
        self.assertTrue(expected.issubset(tools))
        self.assertTrue(tools["delete_stage"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["delete_attribute"]["annotations"]["destructiveHint"])

    def test_protected_stage_is_reported_and_cannot_be_archived(self):
        preview = self._ok(
            "archive_stage",
            {
                "stage_id": str(self.new_stage.id),
                "reason": "Review protected New leads stage lifecycle behavior.",
            },
        )
        self.assertFalse(preview["can_apply"])
        self.assertTrue(preview["protected_object_status"]["protected"])
        self.assertTrue(preview["migration_required"] or preview["protected_object_status"]["protected"])

    def test_custom_stage_archives_restores_and_deletes_when_dependency_free(self):
        stage = self._custom_stage(name="Lifecycle Review")
        preview = self._ok(
            "archive_stage",
            {
                "stage_id": str(stage.id),
                "reason": "Archive dependency-free Lifecycle Review stage.",
            },
        )
        self.assertTrue(preview["can_apply"])
        self.assertTrue(preview["reversible"])
        archived = self._ok(
            "archive_stage",
            {
                "stage_id": str(stage.id),
                "dry_run": False,
                "approved": False,
                "reason": "Archive dependency-free Lifecycle Review stage.",
            },
        )
        self.assertEqual(archived["status"], "ARCHIVED")
        stage.refresh_from_db()
        self.assertFalse(stage.is_active)

        restored = self._ok(
            "upsert_stage_configuration",
            {
                "pipeline_id": str(self.pipeline.id),
                "stage_id": str(stage.id),
                "dry_run": False,
                "approved": False,
                "reason": "Restore Lifecycle Review stage after archive verification.",
                "data": {
                    "name": stage.name,
                    "description": stage.description,
                    "display_order": stage.display_order,
                    "is_active": True,
                    "ai_on": True,
                },
            },
        )
        self.assertEqual(restored["status"], "FIXED")
        stage.refresh_from_db()
        self.assertTrue(stage.is_active)

        delete_preview = self._ok(
            "delete_stage",
            {
                "stage_id": str(stage.id),
                "reason": "Permanently remove dependency-free Lifecycle Review stage.",
            },
        )
        self.assertTrue(delete_preview["can_apply"])
        self.assertFalse(delete_preview["reversible"])
        deleted = self._ok(
            "delete_stage",
            {
                "stage_id": str(stage.id),
                "dry_run": False,
                "approved": False,
                "reason": "Permanently remove dependency-free Lifecycle Review stage.",
            },
        )
        self.assertEqual(deleted["status"], "DELETED")
        self.assertFalse(Stage.objects.filter(pk=stage.id).exists())

    def test_attribute_archive_blocks_references_preserves_values_and_can_restore(self):
        attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Preferred Table",
            key="preferred_table",
            field_type="text",
            description="Restaurant seating preference.",
        )
        self.lead.attributes = {"preferred_table": "Window"}
        self.lead.save(update_fields=["attributes"])
        self.qualified.config = {
            **(self.qualified.config or {}),
            "required_attribute_ids": [str(attribute.id)],
        }
        self.qualified.save(update_fields=["config", "updated_at"])

        blocked = self._ok(
            "archive_attribute",
            {
                "attribute_id": str(attribute.id),
                "reason": "Review archive dependencies for Preferred Table attribute.",
            },
        )
        self.assertFalse(blocked["can_apply"])
        self.assertTrue(blocked["migration_required"])
        self.assertEqual(
            blocked["affected_records"]["lead_values"],
            1,
        )

        self.qualified.config = {
            **(self.qualified.config or {}),
            "required_attribute_ids": [],
        }
        self.qualified.save(update_fields=["config", "updated_at"])

        archived = self._ok(
            "archive_attribute",
            {
                "attribute_id": str(attribute.id),
                "dry_run": False,
                "approved": False,
                "reason": "Archive unused Preferred Table attribute while retaining history.",
            },
        )
        self.assertEqual(archived["status"], "ARCHIVED")
        attribute.refresh_from_db()
        self.assertFalse(attribute.is_active)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["preferred_table"], "Window")

        rejected = self._call(
            "update_lead_attributes",
            {
                "lead_id": str(self.lead.id),
                "values": {"preferred_table": "Booth"},
                "reason": "Verify archived CRM attributes reject new writes.",
            },
        )
        self.assertTrue(rejected["isError"])

        restored = self._ok(
            "upsert_attribute_configuration",
            {
                "attribute_id": str(attribute.id),
                "dry_run": False,
                "approved": False,
                "reason": "Restore Preferred Table CRM attribute after archive test.",
                "data": {
                    "name": attribute.name,
                    "field_type": attribute.field_type,
                    "description": attribute.description,
                    "options": [],
                },
            },
        )
        self.assertEqual(restored["status"], "FIXED")
        attribute.refresh_from_db()
        self.assertTrue(attribute.is_active)

    def test_attribute_archive_detects_whatsapp_template_placeholder_dependency(self):
        attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="VIP Note",
            key="vip_note",
            field_type="text",
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Template API",
            phone_number_id="template-phone-id",
            display_phone_number="+919800000003",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        template = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=account,
            name="vip_note_template",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Your note is {{vip_note}}",
            created_by=self.admin,
        )

        blocked = self._ok(
            "archive_attribute",
            {
                "attribute_id": str(attribute.id),
                "reason": "Review VIP Note attribute template dependencies before archive.",
            },
        )
        self.assertFalse(blocked["can_apply"])
        self.assertEqual(
            len(blocked["dependencies"]["whatsapp_template_references"]),
            1,
        )

        template.status = WhatsAppTemplate.Status.ARCHIVED
        template.save(update_fields=["status", "updated_at"])
        clear = self._ok(
            "archive_attribute",
            {
                "attribute_id": str(attribute.id),
                "reason": "Recheck VIP Note after retiring dependent WhatsApp template.",
            },
        )
        self.assertTrue(clear["can_apply"])

    def test_attribute_delete_requires_explicit_bounded_value_purge(self):
        attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Legacy Preference",
            key="legacy_preference",
            field_type="text",
        )
        self.lead.attributes = {"legacy_preference": "Old"}
        self.lead.save(update_fields=["attributes"])

        preview = self._ok(
            "delete_attribute",
            {
                "attribute_id": str(attribute.id),
                "reason": "Review permanent deletion of Legacy Preference attribute.",
            },
        )
        self.assertFalse(preview["can_apply"])
        self.assertEqual(preview["affected_records"]["lead_values"], 1)
        self.assertTrue(preview["migration_required"])

        deleted = self._ok(
            "delete_attribute",
            {
                "attribute_id": str(attribute.id),
                "purge_values": True,
                "dry_run": False,
                "approved": False,
                "reason": "Permanently remove Legacy Preference and its bounded lead values.",
            },
        )
        self.assertEqual(deleted["status"], "DELETED")
        self.assertEqual(deleted["purged_lead_values"], 1)
        self.assertFalse(AttributeDefinition.objects.filter(pk=attribute.id).exists())
        self.lead.refresh_from_db()
        self.assertNotIn("legacy_preference", self.lead.attributes)

    def test_workflow_archive_disables_runtime_cancels_pending_and_restores_rule(self):
        workflow = SmartTrigger.objects.create(
            organization=self.organization,
            name="Lifecycle AI Workflow",
            enabled=True,
            is_active=True,
            position=1,
            trigger_type="lead_created",
            conditions={
                "scopes": [
                    {
                        "pipeline": str(self.pipeline.id),
                        "stages": [str(self.new_stage.id)],
                    }
                ],
                "sources": [],
                "attributes": [],
            },
            action_type="ai",
            action={"enabled": True},
            fingerprint="lifecycle-workflow-fingerprint",
            created_by=self.admin,
        )
        event = TriggerEvent.objects.create(
            organization=self.organization,
            lead=self.lead,
            kind="lead_created",
            key="lifecycle-workflow-event",
            payload={},
        )
        run = TriggerRun.objects.create(
            rule=workflow,
            event=event,
            lead=self.lead,
            action_type="ai",
            action={"enabled": True},
            status="pending",
            due_at=timezone.now(),
        )
        in_flight_event = TriggerEvent.objects.create(
            organization=self.organization,
            lead=self.lead,
            kind="lead_created",
            key="lifecycle-workflow-in-flight-event",
            payload={},
        )
        in_flight = TriggerRun.objects.create(
            rule=workflow,
            event=in_flight_event,
            lead=self.lead,
            action_type="message",
            action={"body": "Potentially in-flight"},
            status="dispatching",
            due_at=timezone.now(),
        )

        preview = self._ok(
            "archive_workflow",
            {
                "workflow_id": str(workflow.id),
                "reason": "Archive Workflow and cancel its pending execution safely.",
            },
        )
        self.assertTrue(preview["can_apply"])
        self.assertFalse(preview["reversible"])
        self.assertEqual(preview["affected_records"]["pending_or_queued_runs"], 1)
        self.assertEqual(preview["affected_records"]["in_flight_runs"], 1)

        archived = self._ok(
            "archive_workflow",
            {
                "workflow_id": str(workflow.id),
                "dry_run": False,
                "approved": False,
                "reason": "Archive Workflow and cancel its pending execution safely.",
            },
        )
        self.assertEqual(archived["status"], "ARCHIVED")
        workflow.refresh_from_db()
        run.refresh_from_db()
        in_flight.refresh_from_db()
        self.assertFalse(workflow.is_active)
        self.assertFalse(workflow.enabled)
        self.assertEqual(run.status, "skipped")
        self.assertEqual(in_flight.status, "needs_review")

        restored = self._ok(
            "upsert_workflow_configuration",
            {
                "workflow_id": str(workflow.id),
                "dry_run": False,
                "approved": False,
                "reason": "Restore Lifecycle AI Workflow after archive test.",
                "data": {
                    "name": workflow.name,
                    "enabled": True,
                    "trigger_type": workflow.trigger_type,
                    "conditions": workflow.conditions,
                    "action_type": workflow.action_type,
                    "action": workflow.action,
                },
            },
        )
        self.assertEqual(restored["status"], "FIXED")
        workflow.refresh_from_db()
        run.refresh_from_db()
        in_flight.refresh_from_db()
        self.assertTrue(workflow.is_active)
        self.assertTrue(workflow.enabled)
        self.assertEqual(run.status, "skipped")
        self.assertEqual(in_flight.status, "needs_review")

    def test_cadence_archive_blocks_active_leads_then_restores(self):
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Lifecycle API",
            phone_number_id="123456789",
            display_phone_number="+919800000002",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        cadence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Lifecycle Cadence",
            description="Lifecycle test cadence.",
            whatsapp_account=account,
            created_by=self.admin,
        )
        state = LeadSequenceState.objects.create(
            organization=self.organization,
            lead=self.lead,
            sequence=cadence,
            assigned_by=self.admin,
            status=LeadSequenceState.Status.ACTIVE,
        )

        blocked = self._ok(
            "archive_cadence",
            {
                "cadence_id": str(cadence.id),
                "reason": "Review Cadence archive while a lead is still active.",
            },
        )
        self.assertFalse(blocked["can_apply"])
        self.assertEqual(
            blocked["affected_records"]["active_or_paused_lead_states"],
            1,
        )

        state.status = LeadSequenceState.Status.COMPLETED
        state.completed_at = timezone.now()
        state.save(update_fields=["status", "completed_at", "updated_at"])

        archived = self._ok(
            "archive_cadence",
            {
                "cadence_id": str(cadence.id),
                "dry_run": False,
                "approved": False,
                "reason": "Archive Cadence after all active lead states are completed.",
            },
        )
        self.assertEqual(archived["status"], "ARCHIVED")
        cadence.refresh_from_db()
        self.assertFalse(cadence.is_active)

        restored = self._ok(
            "upsert_cadence_configuration",
            {
                "cadence_id": str(cadence.id),
                "dry_run": False,
                "approved": False,
                "reason": "Restore Lifecycle Cadence after archive verification.",
                "data": {
                    "name": cadence.name,
                    "description": cadence.description,
                    "provider": "api",
                    "whatsapp_account_id": str(account.id),
                    "is_active": True,
                },
            },
        )
        self.assertEqual(restored["status"], "FIXED")
        cadence.refresh_from_db()
        self.assertTrue(cadence.is_active)

    def test_pipeline_archive_blocks_leads_and_archives_empty_pipeline(self):
        blocked = self._ok(
            "archive_pipeline",
            {
                "pipeline_id": str(self.pipeline.id),
                "reason": "Review Pipeline archive while it still contains a lead.",
            },
        )
        self.assertFalse(blocked["can_apply"])
        self.assertEqual(blocked["affected_records"]["leads"], 1)

        empty = Pipeline.objects.create(
            organization=self.organization,
            name="Empty Lifecycle Pipeline",
        )
        preview = self._ok(
            "archive_pipeline",
            {
                "pipeline_id": str(empty.id),
                "reason": "Archive dependency-free Empty Lifecycle Pipeline.",
            },
        )
        self.assertTrue(preview["can_apply"])
        archived = self._ok(
            "archive_pipeline",
            {
                "pipeline_id": str(empty.id),
                "dry_run": False,
                "approved": False,
                "reason": "Archive dependency-free Empty Lifecycle Pipeline.",
            },
        )
        self.assertEqual(archived["status"], "ARCHIVED")
        empty.refresh_from_db()
        self.assertFalse(empty.is_active)

        restored = self._ok(
            "upsert_pipeline_configuration",
            {
                "pipeline_id": str(empty.id),
                "dry_run": False,
                "approved": False,
                "reason": "Restore Empty Lifecycle Pipeline after archive verification.",
                "data": {
                    "name": empty.name,
                    "description": empty.description,
                    "is_active": True,
                    "ai_enabled": True,
                },
            },
        )
        self.assertEqual(restored["status"], "FIXED")
        empty.refresh_from_db()
        self.assertTrue(empty.is_active)

    def test_touchpoint_and_faq_archive_dry_runs_return_lifecycle_contract(self):
        category = TouchpointCategory.objects.create(
            organization=self.organization,
            name="Lifecycle Replies",
        )
        reply = TouchpointReply.objects.create(
            category=category,
            title="Lifecycle reply",
            body="Saved reply body.",
        )
        faq = FAQ.objects.create(
            organization=self.organization,
            question="Lifecycle FAQ?",
            answer="Lifecycle answer.",
        )

        touchpoint = self._ok(
            "archive_touchpoint",
            {
                "touchpoint_id": str(reply.id),
                "reason": "Review Touchpoint archive lifecycle metadata.",
            },
        )
        faq_preview = self._ok(
            "archive_faq",
            {
                "faq_id": str(faq.id),
                "reason": "Review FAQ archive lifecycle metadata.",
            },
        )
        for preview in (touchpoint, faq_preview):
            self.assertIn("dependencies", preview)
            self.assertIn("affected_records", preview)
            self.assertIn("protected_object_status", preview)
            self.assertIn("migration_requirements", preview)
            self.assertIn("reversible", preview)
            self.assertTrue(preview["can_apply"])
