import base64
import json
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_engagement.models import Document, FAQ, OrgInfo
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.followups.models import (
    FollowupSequence,
    FollowupStep,
    FollowupStepAttachment,
    TouchpointReply,
)
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
            "get_content_authoring_policy",
            "get_qualification_configuration",
            "validate_qualification_configuration",
            "upsert_qualification_configuration",
            "list_whatsapp_accounts",
            "list_whatsapp_templates",
            "get_whatsapp_template_status",
            "create_whatsapp_template",
            "submit_whatsapp_template",
            "submit_whatsapp_templates",
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

    def test_content_authoring_policy_exposes_tenant_supported_placeholders(self):
        policy = self._call("get_content_authoring_policy")
        self.assertTrue(policy["plain_text_only"])
        self.assertEqual(policy["placeholder_syntax"], "{{placeholder_key}}")
        keys = {item["key"] for item in policy["placeholders"]}
        self.assertIn("lead_first_name", keys)
        self.assertIn("running_ads", keys)
        self.assertNotIn("org_id", keys)

    def test_operations_content_is_normalized_before_persistence(self):
        touchpoint = self._call(
            "upsert_touchpoint",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Create normalized restaurant saved reply.",
                "data": {
                    "category_name": "**Reservations**",
                    "title": "**Welcome**",
                    "body": "<b>Hi</b> **{lead_first_name}**, _welcome_.",
                },
            },
        )["touchpoint"]
        reply = TouchpointReply.objects.get(pk=touchpoint["id"])
        self.assertEqual(reply.category.name, "Reservations")
        self.assertEqual(reply.title, "Welcome")
        self.assertEqual(reply.body, "Hi {{lead_first_name}}, welcome.")

        hosted = self._call(
            "add_hosted_whatsapp_step",
            {
                "dry_run": False,
                "approved": False,
                "reason": "Create normalized Hosted restaurant follow-up.",
                "cadence_id": str(self.cadence.id),
                "data": {
                    "title": "**Follow up**",
                    "body": "<p>Hello {lead_first_name}</p> **Checking in**",
                    "schedule": {"type": "immediate"},
                },
            },
        )["step"]
        hosted_row = HostedFollowupStepConfig.objects.get(
            step_id=hosted["id"],
        )
        self.assertEqual(hosted_row.step.title, "Follow up")
        self.assertEqual(
            hosted_row.body,
            "Hello {{lead_first_name}}\nChecking in",
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

    def test_hosted_attachment_over_512_kib_can_be_uploaded_through_operations_mcp(self):
        raw = b"h" * (2 * 1024 * 1024)
        encoded = base64.b64encode(raw).decode("ascii")
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            result = self._call(
                "add_hosted_whatsapp_step",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Attach restaurant brochure to Hosted follow-up.",
                    "cadence_id": str(self.cadence.id),
                    "data": {
                        "title": "Restaurant brochure",
                        "body": "Here is our restaurant brochure.",
                        "attachment_name": "brochure.pdf",
                        "attachment_mime_type": "application/pdf",
                        "attachment_base64": encoded,
                        "schedule": {"type": "immediate"},
                    },
                },
            )
            step = FollowupStep.objects.get(pk=result["step"]["id"])
            hosted = HostedFollowupStepConfig.objects.get(step=step)
            self.assertEqual(hosted.attachment_size, len(raw))
            self.assertEqual(hosted.attachment_original_name, "brochure.pdf")
            self.assertTrue(hosted.attachment)

    def test_email_cadence_attachments_can_be_created_replaced_and_removed(self):
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            created = self._call(
                "add_cadence_step",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Add reservation email with menu attachment.",
                    "cadence_id": str(self.cadence.id),
                    "data": {
                        "type": "email",
                        "title": "Reservation email",
                        "subject": "Your reservation",
                        "body": "Please review the attached menu.",
                        "attachments": [
                            {
                                "name": "menu.pdf",
                                "mime_type": "application/pdf",
                                "content_base64": base64.b64encode(b"menu-v1").decode("ascii"),
                            }
                        ],
                        "schedule": {"type": "immediate"},
                    },
                },
            )
            step = FollowupStep.objects.get(pk=created["step"]["id"])
            attachment = FollowupStepAttachment.objects.get(step=step)
            self.assertEqual(attachment.original_name, "menu.pdf")
            self.assertEqual(attachment.size, len(b"menu-v1"))

            updated = self._call(
                "update_cadence_step",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Replace reservation email attachment.",
                    "cadence_id": str(self.cadence.id),
                    "step_id": str(step.id),
                    "data": {
                        "title": "Reservation email",
                        "subject": "Your reservation",
                        "body": "Please review the updated menu.",
                        "attachments": [
                            {
                                "name": "updated-menu.pdf",
                                "mime_type": "application/pdf",
                                "content_base64": base64.b64encode(b"menu-v2").decode("ascii"),
                            }
                        ],
                        "schedule": {"type": "immediate"},
                    },
                },
            )
            self.assertEqual(
                updated["step"]["email_attachments"][0]["name"],
                "updated-menu.pdf",
            )
            self.assertEqual(step.attachments.count(), 1)
            self.assertEqual(
                step.attachments.get().original_name,
                "updated-menu.pdf",
            )

            removed = self._call(
                "update_cadence_step",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Remove reservation email attachment.",
                    "cadence_id": str(self.cadence.id),
                    "step_id": str(step.id),
                    "data": {
                        "title": "Reservation email",
                        "subject": "Your reservation",
                        "body": "No attachment is needed now.",
                        "remove_attachments": True,
                        "schedule": {"type": "immediate"},
                    },
                },
            )
            self.assertEqual(removed["step"]["email_attachments"], [])
            self.assertFalse(step.attachments.exists())

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
        self.assertTrue(connected["safe_status_only"])
        self.assertNotIn("access_token", json.dumps(connected))
        self.assertNotIn('"qr":', json.dumps(connected).lower())

    def test_meta_template_can_be_created_and_submitted_through_operations_mcp(self):
        self.account.waba_id = "waba-restaurant-1"
        self.account.access_token = "meta-secret-token"
        self.account.save(update_fields=["waba_id", "access_token", "updated_at"])

        create_args = {
            "dry_run": False,
            "approved": False,
            "reason": "Create restaurant reservation confirmation template.",
            "whatsapp_account_id": str(self.account.id),
            "pipeline_id": str(self.pipeline.id),
            "name": "reservation_confirmation",
            "body": "Your reservation is confirmed.",
            "category": "utility",
            "language": "en_US",
        }
        created = self._call("create_whatsapp_template", create_args)
        self.assertEqual(created["status"], "CREATED")
        template = WhatsAppTemplate.objects.get(pk=created["template"]["id"])
        self.assertEqual(template.account_id, self.account.id)
        self.assertEqual(template.organization_id, self.organization.id)
        self.assertEqual(template.status, WhatsAppTemplate.Status.DRAFT)
        self.assertNotIn("meta-secret-token", json.dumps(created))

        repeated = self._call("create_whatsapp_template", create_args)
        self.assertEqual(repeated["status"], "NO_CHANGE")
        self.assertTrue(repeated["idempotent"])

        with patch(
            "services.channels.template_service.WhatsAppClient._post",
            return_value={"id": "meta-template-42", "status": "PENDING"},
        ):
            submitted = self._call(
                "submit_whatsapp_template",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Submit reservation confirmation template to Meta.",
                    "template_id": str(template.id),
                },
            )

        self.assertEqual(submitted["status"], "SUBMITTED")
        self.assertTrue(submitted["submitted_to_meta"])
        template.refresh_from_db()
        self.assertEqual(template.meta_template_id, "meta-template-42")
        self.assertEqual(template.status, WhatsAppTemplate.Status.PENDING)

        status = self._call(
            "get_whatsapp_template_status",
            {"template_id": str(template.id)},
        )["template"]
        self.assertEqual(status["status"], WhatsAppTemplate.Status.PENDING)
        self.assertEqual(status["meta_template_id"], "meta-template-42")

        listed = self._call(
            "list_whatsapp_templates",
            {"whatsapp_account_id": str(self.account.id)},
        )
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["templates"][0]["id"], str(template.id))
        self.assertNotIn("meta-secret-token", json.dumps(listed))

    def test_multiple_meta_templates_can_be_submitted_in_one_operations_call(self):
        self.account.waba_id = "waba-batch-1"
        self.account.access_token = "meta-batch-secret"
        self.account.save(update_fields=["waba_id", "access_token", "updated_at"])
        first = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            created_by=self.admin,
            name="batch_one",
            body="First batch template",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.DRAFT,
        )
        second = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            created_by=self.admin,
            name="batch_two",
            body="Second batch template",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.DRAFT,
        )

        responses = [
            {"id": "meta-batch-one", "status": "PENDING"},
            {"id": "meta-batch-two", "status": "PENDING"},
        ]
        with patch(
            "services.channels.template_service.WhatsAppClient._post",
            side_effect=responses,
        ):
            result = self._call(
                "submit_whatsapp_templates",
                {
                    "dry_run": False,
                    "approved": False,
                    "reason": "Submit restaurant Meta templates in one batch.",
                    "template_ids": [str(first.id), str(second.id)],
                },
            )

        self.assertEqual(result["status"], "SUBMITTED")
        self.assertEqual(result["submitted_count"], 2)
        self.assertEqual(result["failed_count"], 0)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.meta_template_id, "meta-batch-one")
        self.assertEqual(second.meta_template_id, "meta-batch-two")
        self.assertNotIn("meta-batch-secret", json.dumps(result))

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
