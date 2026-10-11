"""Regression coverage for complete, tenant-safe account-handover reads."""

import json
import uuid

from django.test import SimpleTestCase
from django.utils import timezone

from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition
from apps.followups.models import FollowupSequence
from apps.integrations.diagnostic_auth import sanitize_data
from apps.integrations.models import OperationsPolicy
from apps.integrations.operations.response_policy import sanitize_tool_response
from apps.integrations.operations_auth import OPERATIONS_READ_SCOPE
from apps.integrations.operations_policy import (
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
)
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase
from apps.triggers.models import SmartTrigger


class TestHandoverResponseSanitation(SimpleTestCase):
    def test_deeper_read_preserves_scope_ids_and_values_without_changing_defaults(self):
        payload = {
            "workflows": [{
                "conditions": {
                    "scopes": [{"pipeline": "pipeline", "stages": ["stage"]}],
                    "attributes": [{"key": "interest", "values": ["Bike"]}],
                },
            }],
        }
        bounded = sanitize_data(payload)
        self.assertEqual(
            bounded["workflows"][0]["conditions"]["scopes"][0]["stages"],
            ["[TRUNCATED]"],
        )
        for name in ("get_automation_configuration", "get_channel_cadence_configuration"):
            with self.subTest(tool=name):
                safe = sanitize_tool_response(name, payload)
                self.assertEqual(safe["workflows"], payload["workflows"])
                self.assertFalse(safe["response_truncated"])
                self.assertFalse(safe["response_redacted"])

    def test_depth_override_is_propagated_and_capped(self):
        payload = "visible"
        for _ in range(22):
            payload = {"child": payload}
        capped = sanitize_data(payload, max_depth=999)
        for _ in range(21):
            capped = capped["child"]
        self.assertEqual(capped, "[TRUNCATED]")

    def test_long_text_and_nested_list_loss_have_paths_without_removed_values(self):
        payload = {
            "workflows": [{
                "conditions": {"keywords": [str(index) for index in range(101)]},
                "action": {
                    "body": "Safe customer reminder. " * 1000,
                    "note": "password: private-value",
                },
            }],
        }
        safe = sanitize_tool_response("get_automation_configuration", payload)
        self.assertTrue(safe["response_truncated"])
        self.assertTrue(safe["response_redacted"])
        changes = safe["response_sanitization"]["paths"]
        self.assertIn(
            {"path": "/workflows/0/conditions/keywords", "kind": "list", "omitted_count": 1},
            changes,
        )
        self.assertIn({"path": "/workflows/0/action/body", "kind": "text"}, changes)
        self.assertIn({"path": "/workflows/0/action/note", "kind": "redacted"}, changes)
        self.assertNotIn("private-value", json.dumps(safe))
        self.assertEqual(len(safe["workflows"][0]["conditions"]["keywords"]), 100)

    def test_sanitization_path_metadata_is_bounded(self):
        payload = {"action": {f"password_{index}": "private-value" for index in range(150)}}
        safe = sanitize_tool_response("get_automation_configuration", payload)
        report = safe["response_sanitization"]
        self.assertEqual(report["change_count"], 150)
        self.assertEqual(len(report["paths"]), 100)
        self.assertTrue(report["paths_truncated"])
        self.assertNotIn("private-value", json.dumps(safe))

    def test_generated_handover_chunks_are_not_resanitized(self):
        # This policy branch relies on the dedicated renderer's strict input
        # validation; output must remain byte-for-byte compatible with its hash.
        artifact = {"chunk": "<p>Reviewed handover</p>" * 200, "sha256": "a" * 64}
        self.assertIs(sanitize_tool_response("render_account_handover", artifact), artifact)


class TestOperationsHandoverReads(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
        )
        self.bearer = self._token(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            scopes=[OPERATIONS_READ_SCOPE],
        )
        self.sender = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Handover Sender",
            display_phone_number="+919000000060",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _workflow(self, *, organization=None, **changes):
        organization = organization or self.organization
        values = {
            "organization": organization,
            "name": "Handover Workflow",
            "enabled": True,
            "position": 1,
            "trigger_type": "keyword",
            "conditions": {"scopes": [], "attributes": [], "keywords": ["hello"]},
            "action_type": "ai",
            "action": {"enabled": False},
            "fingerprint": uuid.uuid4().hex * 2,
            "created_by": self.admin if organization == self.organization else self.other_admin,
        }
        values.update(changes)
        return SmartTrigger.objects.create(**values)

    def _read(self, arguments=None):
        result = self._result(self._call(
            self.bearer, "get_automation_configuration", arguments or {},
        ))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_mcp_preserves_nested_scope_attribute_values_and_long_message(self):
        attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Interested Model",
            key="interested_model",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        body = "Please review the requested bike model with our team. " * 40
        rule = self._workflow(
            conditions={
                "scopes": [{
                    "pipeline": str(self.pipeline.id),
                    "stages": [str(self.new_stage.id), str(self.review_stage.id)],
                }],
                "attributes": [{"key": attribute.key, "match": "equals", "values": ["Pulsar"]}],
                "keywords": ["bike"],
            },
            action_type="message",
            action={
                "account": str(self.sender.id),
                "body": body,
                "schedule": "relative",
                "duration": 1,
                "unit": "hours",
            },
        )
        data = self._read({"workflow_id": str(rule.id)})
        row = data["workflows"][0]
        self.assertEqual(row["conditions"], rule.conditions)
        self.assertEqual(row["action"]["body"], body)
        self.assertEqual(row["id"], str(rule.id))
        self.assertFalse(data["response_truncated"])
        self.assertFalse(data["response_redacted"])
        self.assertIsNone(data["pagination"]["next_workflow_offset"])

    def test_mcp_pages_all_workflows_and_independent_cadences_with_stable_ties(self):
        rules = [
            self._workflow(name=f"Workflow {index:03d}", position=1)
            for index in range(103)
        ]
        same_time = timezone.now()
        SmartTrigger.objects.filter(id__in=[rule.id for rule in rules]).update(created_at=same_time)
        sequences = [
            FollowupSequence.objects.create(
                organization=self.organization,
                created_by=self.admin,
                name=f"Cadence {index}",
                whatsapp_account=self.sender,
            )
            for index in range(3)
        ]
        FollowupSequence.objects.filter(id__in=[item.id for item in sequences]).update(
            updated_at=same_time,
        )
        expected_workflows = sorted(str(rule.id) for rule in rules)
        expected_cadences = sorted(str(item.id) for item in sequences)
        seen = []
        offset = 0
        for _ in range(3):
            data = self._read({"limit": 50, "workflow_offset": offset})
            seen.extend(row["id"] for row in data["workflows"])
            offset = data["pagination"]["next_workflow_offset"]
            if offset is None:
                break
        self.assertIsNone(offset)
        self.assertEqual(seen, expected_workflows)
        self.assertEqual(len(set(seen)), 103)
        page = self._read({"limit": 2, "workflow_offset": 100, "cadence_offset": 1})
        self.assertEqual([row["id"] for row in page["workflows"]], expected_workflows[100:102])
        self.assertEqual([row["id"] for row in page["cadences"]], expected_cadences[1:])
        self.assertEqual(page["pagination"]["next_workflow_offset"], 102)
        self.assertIsNone(page["pagination"]["next_cadence_offset"])
        self.assertEqual(page["counts"]["workflow_count"], 103)
        self.assertEqual(page["counts"]["cadence_count"], 3)

    def test_mcp_workflow_lookup_is_exact_and_tenant_isolated(self):
        first = self._workflow(name="Selected")
        self._workflow(name="Unselected")
        foreign = self._workflow(organization=self.other_organization, name="Foreign private name")
        selected = self._read({"workflow_id": str(first.id)})
        self.assertEqual([row["id"] for row in selected["workflows"]], [str(first.id)])
        for target in (foreign.id, uuid.uuid4()):
            with self.subTest(target=target):
                result = self._result(self._call(
                    self.bearer, "get_automation_configuration", {"workflow_id": str(target)},
                ))
                self.assertTrue(result["isError"])
                self.assertNotIn(str(foreign.id), json.dumps(result))
                self.assertNotIn(foreign.name, json.dumps(result))

    def test_mcp_rejects_invalid_offsets_and_lookup_offset_combination(self):
        rule = self._workflow()
        for arguments in (
            {"workflow_offset": -1},
            {"cadence_offset": -1},
            {"workflow_id": "not-a-uuid"},
            {"workflow_id": str(rule.id), "workflow_offset": 1},
        ):
            with self.subTest(arguments=arguments):
                result = self._result(self._call(
                    self.bearer, "get_automation_configuration", arguments,
                ))
                self.assertTrue(result["isError"])

    def test_mcp_preserves_semantic_sensitive_field_redaction(self):
        attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="API Key",
            key="api_key",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        rule = self._workflow(
            conditions={
                "scopes": [],
                "attributes": [{
                    "key": attribute.key, "match": "equals", "values": ["private-condition"],
                }],
            },
            action_type="attribute",
            action={"key": attribute.key, "value": "private-action"},
        )
        data = self._read({"workflow_id": str(rule.id)})
        self.assertTrue(data["response_redacted"])
        self.assertGreaterEqual(data["workflows"][0]["sensitive_fields_redacted"], 2)
        self.assertGreaterEqual(
            data["response_sanitization"]["semantic_workflow_fields_redacted"], 2,
        )
        serialized = json.dumps(data)
        self.assertNotIn("private-condition", serialized)
        self.assertNotIn("private-action", serialized)
        self.assertNotIn('"api_key"', serialized)

    def test_mcp_lookup_keeps_foreign_reference_guard(self):
        rule = self._workflow(conditions={
            "scopes": [{
                "pipeline": str(self.other_lead.pipeline_id),
                "stages": [str(self.other_lead.stage_id)],
            }],
            "attributes": [],
            "keywords": ["hello"],
        })
        result = self._result(self._call(
            self.bearer, "get_automation_configuration", {"workflow_id": str(rule.id)},
        ))
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["status"], "NOT_ALLOWED")
        self.assertNotIn(str(self.other_lead.pipeline_id), json.dumps(result))
        self.assertNotIn(str(self.other_lead.stage_id), json.dumps(result))
