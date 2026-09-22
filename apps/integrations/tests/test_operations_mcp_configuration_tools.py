import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_engagement.models import Document, FAQ, OrgInfo
from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.followups.models import FollowupSequence, FollowupStep, TouchpointReply
from apps.hosted_automation.models import HostedFollowupStepConfig
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
    CAP_CADENCE_CONFIG_WRITE,
    CAP_DIAGNOSTICS_READ,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    CAP_WORKFLOW_CONFIG_WRITE,
    ROLE_ORGANIZATION_ADMIN,
    capabilities_for_grant,
)
from apps.organizations.features import set_hosted_account_enabled
from apps.organizations.models import Organization


class OperationsMCPConfigurationToolsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="P0 Configuration Org")
        set_hosted_account_enabled(self.organization, True)
        self.admin = User.objects.create_user(
            email="p0-admin@example.test",
            organization=self.organization,
            password=None,
            name="P0 Admin",
            role=User.Role.ADMIN,
        )
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_DIAGNOSTICS_READ,
                CAP_AI_CONFIG_WRITE,
                CAP_CADENCE_CONFIG_WRITE,
                CAP_MESSAGING_CONFIG_WRITE,
                CAP_WORKFLOW_CONFIG_WRITE,
            ],
            approval_required_capabilities=[],
            updated_by=self.admin,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Restaurant Sales",
            country_code="+91",
            phone_number="9999999991",
        )
        self.new_stage = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Restaurant Lead",
            phone="+919111111111",
        )
        self.running_ads = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Running Ads",
            key="running_ads",
            field_type="text",
            description="Whether the lead currently runs ads.",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Restaurant Hosted",
            phone_number_id="+919999999991",
            display_phone_number="+919999999991",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.cadence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Restaurant Hosted Cadence",
            description="Hosted follow-up",
            whatsapp_account=self.account,
            created_by=self.admin,
        )
        self.oauth_client = OperationsOAuthClient.objects.create(
            client_id="p0_configuration_test_client",
            client_name="P0 Configuration Test",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        self.bearer = self._token()

    def _token(self):
        raw = "p0-configuration-bearer"
        scopes = [OPERATIONS_READ_SCOPE, OPERATIONS_WRITE_SCOPE]
        granted = sorted(
            capabilities_for_grant(
                role=ROLE_ORGANIZATION_ADMIN,
                organization=self.organization,
                allow_writes=True,
            )
        )
        OperationsOAuthToken.objects.create(
            client=self.oauth_client,
            actor=self.admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash(raw),
            refresh_token_hash=token_hash("p0-configuration-refresh"),
            scope=" ".join(scopes),
            granted_capabilities=granted,
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=4),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        return raw

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
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _tools(self):
        response = self.client.post(
            "/operations/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "tools-list",
                    "method": "tools/list",
                    "params": {},
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.bearer,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]["tools"]

    def test_p0_tools_are_discoverable_with_typed_workflow_discovery(self):
        tools = {item["name"]: item for item in self._tools()}
        expected = {
            "get_qualification_configuration",
            "validate_qualification_configuration",
            "upsert_qualification_configuration",
            "list_whatsapp_accounts",
            "begin_whatsapp_connection",
            "bind_whatsapp_account_to_pipeline",
            "validate_whatsapp_routing",
            "add_hosted_whatsapp_step",
            "update_cadence_step",
            "delete_cadence_step",
            "reorder_cadence_steps",
            "list_touchpoints",
            "upsert_touchpoint",
            "archive_touchpoint",
            "list_faqs",
            "upsert_faq",
            "archive_faq",
            "list_workflow_triggers",
            "list_workflow_actions",
            "get_workflow_schema",
            "validate_workflow_configuration",
            "create_knowledge_source",
            "upload_knowledge_document",
            "publish_knowledge_document",
            "archive_knowledge_document",
            "simulate_ai_conversation",
            "simulate_workflow",
            "simulate_cadence",
        }
        self.assertTrue(expected.issubset(tools))
        self.assertTrue(tools["delete_cadence_step"]["annotations"]["destructiveHint"])

        schema = self._call("get_workflow_schema", {"trigger_type": "lead_created", "action_type": "ai"})
        self.assertIn("lead_created", schema["trigger_schemas"])
        self.assertIn("ai", schema["action_schemas"])
        self.assertEqual(
            schema["action_schemas"]["ai"]["properties"]["enabled"]["type"],
            "boolean",
        )

    def test_structured_qualification_can_validate_apply_and_simulate_without_side_effects(self):
        OrgInfo.objects.create(
            organization=self.organization,
            ai_playbook=(
                "## Rules\nAnswer the lead first.\n\n"
                "## Reminder creation logic\nCreate reminders only when explicitly requested."
            ),
        )
        data = {
            "mode": "configured",
            "requirements": [
                {
                    "stable_id": "ads",
                    "question": "Do you currently run ads?",
                    "required": True,
                    "options": [
                        {"key": "A", "value": "Yes"},
                        {"key": "B", "value": "No"},
                    ],
                },
                {
                    "stable_id": "platform",
                    "question": "Which advertising platform do you use?",
                    "required": True,
                    "eligible_when": {
                        "requirement_id": "ads",
                        "operator": "eq",
                        "value": "Yes",
                    },
                },
            ],
            "mappings": [
                {"requirement_id": "ads", "attribute_keys": ["running_ads"]}
            ],
            "criteria": ["All required qualification questions are answered"],
            "target_stage_id": str(self.qualified.id),
            "final_ack": "Thanks — I have what I need.",
        }

        validation = self._call("validate_qualification_configuration", {"data": data})
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["requirement_count"], 2)

        applied = self._call(
            "upsert_qualification_configuration",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Configure restaurant lead qualification flow.",
                "data": data,
            },
        )
        self.assertEqual(applied["status"], "FIXED")
        info = OrgInfo.objects.get(organization=self.organization)
        self.assertIn("Answer the lead first.", info.ai_playbook)
        self.assertIn("[id: ads]", info.ai_playbook)
        self.assertIn("[if: ads = Yes]", info.ai_playbook)
        self.assertIn("Create reminders only when explicitly requested.", info.ai_playbook)

        simulated = self._call("simulate_ai_conversation", {"answers": {"ads": "No"}})
        self.assertTrue(simulated["simulation"])
        self.assertFalse(simulated["side_effects"])
        self.assertEqual(simulated["messages_sent"], 0)
        self.assertTrue(simulated["qualification"]["completed"])
        self.assertTrue(simulated["qualification"]["would_move_to_completion_stage"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_touchpoint_and_faq_lifecycle_is_archive_first(self):
        touchpoint = self._call(
            "upsert_touchpoint",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Create restaurant reservation saved reply.",
                "data": {
                    "category_name": "Reservations",
                    "title": "Table availability",
                    "body": "Please share your preferred date, time, and party size.",
                },
            },
        )["touchpoint"]
        reply = TouchpointReply.objects.get(pk=touchpoint["id"])
        self.assertTrue(reply.is_active)

        archived = self._call(
            "archive_touchpoint",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Retire outdated restaurant saved reply.",
                "touchpoint_id": str(reply.id),
            },
        )
        self.assertFalse(archived["active"])
        reply.refresh_from_db()
        self.assertFalse(reply.is_active)

        faq = self._call(
            "upsert_faq",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Create restaurant opening-hours FAQ.",
                "data": {
                    "question": "What time do you open?",
                    "answer": "We open at 11 AM.",
                    "is_active": True,
                },
            },
        )["faq"]
        faq_row = FAQ.objects.get(pk=faq["id"])
        self.assertTrue(faq_row.is_active)

        archived_faq = self._call(
            "archive_faq",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Archive outdated opening-hours FAQ.",
                "faq_id": str(faq_row.id),
            },
        )
        self.assertFalse(archived_faq["active"])
        faq_row.refresh_from_db()
        self.assertFalse(faq_row.is_active)

    def test_hosted_free_form_cadence_step_can_be_created_updated_and_simulated(self):
        created = self._call(
            "add_hosted_whatsapp_step",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Add restaurant Hosted WhatsApp follow-up.",
                "cadence_id": str(self.cadence.id),
                "data": {
                    "title": "Reservation follow-up",
                    "body": "Would you like me to help reserve a table?",
                    "schedule": {
                        "type": "delay",
                        "delay_value": 2,
                        "delay_unit": "hours",
                    },
                },
            },
        )["step"]
        step = FollowupStep.objects.get(pk=created["id"])
        hosted = HostedFollowupStepConfig.objects.get(step=step)
        self.assertEqual(hosted.body, "Would you like me to help reserve a table?")
        self.assertIsNone(step.whatsapp_template_id)

        updated = self._call(
            "update_cadence_step",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Improve restaurant Hosted follow-up copy.",
                "cadence_id": str(self.cadence.id),
                "step_id": str(step.id),
                "data": {
                    "title": "Reservation assistance",
                    "body": "Can I help you choose a table time?",
                    "schedule": {"type": "delay", "delay_value": 3, "delay_unit": "hours"},
                },
            },
        )["step"]
        self.assertEqual(updated["hosted_body"], "Can I help you choose a table time?")

        simulated = self._call("simulate_cadence", {"cadence_id": str(self.cadence.id)})
        self.assertTrue(simulated["simulation"])
        self.assertEqual(simulated["messages_sent"], 0)
        self.assertEqual(len(simulated["steps"]), 1)

    def test_workflow_validation_and_simulation_use_canonical_validator_without_execution(self):
        data = {
            "name": "Restaurant lead AI",
            "enabled": True,
            "trigger_type": "lead_created",
            "conditions": {
                "scopes": [
                    {
                        "pipeline": str(self.pipeline.id),
                        "stages": [str(self.new_stage.id)],
                    }
                ],
                "sources": [],
                "attributes": [],
            },
            "action_type": "ai",
            "action": {"enabled": True},
        }
        validated = self._call("validate_workflow_configuration", {"data": data})
        self.assertTrue(validated["valid"])
        simulated = self._call(
            "simulate_workflow",
            {"lead_id": str(self.lead.id), "data": data, "event": {}},
        )
        self.assertTrue(simulated["matched"])
        self.assertEqual(simulated["workflow_actions_executed"], 0)

    def test_whatsapp_routing_and_connection_never_return_credentials(self):
        routing = self._call("validate_whatsapp_routing")
        self.assertTrue(routing["valid"])

        with patch("apps.channels.hosted_tasks.initialize_hosted_session_task.delay"):
            connected = self._call(
                "begin_whatsapp_connection",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Initialize restaurant Hosted WhatsApp session.",
                    "country_code": "+91",
                    "phone_number": "9999999991",
                },
            )
        self.assertFalse(connected["credentials_exposed"])
        self.assertNotIn("access_token", json.dumps(connected))
        self.assertNotIn("qr", json.dumps(connected).lower())

    def test_knowledge_document_upload_uses_existing_secure_ingestion_pipeline(self):
        payload = base64.b64encode(b"Restaurant menu and reservation policy.").decode("ascii")
        result = self._call(
            "upload_knowledge_document",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Add restaurant policy to the AI Brain.",
                "data": {
                    "filename": "restaurant-policy.txt",
                    "name": "Restaurant policy",
                    "content_base64": payload,
                },
            },
        )
        self.assertTrue(result["ingestion_queued"])
        document = Document.objects.get(pk=result["document"]["id"])
        self.assertEqual(document.organization_id, self.organization.id)
        self.assertEqual(document.processing_status, Document.ProcessingStatus.PENDING)
