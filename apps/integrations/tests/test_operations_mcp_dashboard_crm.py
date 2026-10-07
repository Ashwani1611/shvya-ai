"""Dashboard parity must preserve approvals, tenant isolation and no-send defaults."""

from uuid import uuid4
from unittest.mock import patch

from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase
from apps.accounts.models import User
from apps.crm.models import AttributeDefinition, Lead, LeadActivity
from apps.integrations.operations_models import OperationsAuditEvent, OperationsPolicy
from apps.integrations.operations_policy import (
    CAP_LEAD_CREATE, CAP_LEAD_IMPORT, CAP_LEAD_READ, CAP_LEAD_STAGE_WRITE,
    CAP_LEAD_WRITE, CAP_ORGANIZATION_CREATE, ROLE_ORGANIZATION_ADMIN, ROLE_SUPERADMIN,
)
from apps.organizations.models import Organization
from apps.triggers.models import TriggerEvent
from services.triggers.evaluator import emit, suppress_workflow_events


class TestOperationsMCPDashboardCRM(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        self.capabilities = [CAP_LEAD_READ, CAP_LEAD_CREATE, CAP_LEAD_WRITE, CAP_LEAD_IMPORT, CAP_LEAD_STAGE_WRITE]
        OperationsPolicy.objects.create(organization=self.organization, organization_admin_enabled=True,
                                        allowed_capabilities=self.capabilities, approval_required_capabilities=self.capabilities)

    def token(self):
        return self._token(actor=self.admin, role=ROLE_ORGANIZATION_ADMIN, organization=self.organization)

    def create_arguments(self, **overrides):
        result = {"pipeline_id": str(self.pipeline.id), "stage_id": str(self.new_stage.id),
                  "data": {"name": "MCP Provisioned", "phone": "+919877665544"},
                  "client_request_id": str(uuid4()), "reason": "Provision the requested CRM lead"}
        result.update(overrides)
        return result

    def approve(self, token, tool, args):
        dry = self._result(self._call(token, tool, args))
        self.assertFalse(dry["isError"], dry)
        return self._result(self._call(token, tool, {**args, "dry_run": False, "approved": True,
                                                    "approval_event_id": dry["structuredContent"]["approval_event_id"]}))

    def test_lead_reads_are_tenant_scoped_and_paginated(self):
        token = self.token()
        result = self._result(self._call(token, "list_crm_leads", {"limit": 1}))
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["leads"][0]["id"], str(self.lead.id))
        self.assertFalse(result["structuredContent"]["has_more"])
        other = self._result(self._call(token, "get_crm_lead", {"lead_id": str(self.other_lead.id)}))
        self.assertTrue(other["isError"])

    def test_lead_read_requires_fresh_capability_grant(self):
        token = self._token(actor=self.admin, role=ROLE_ORGANIZATION_ADMIN,
                            organization=self.organization, granted_capabilities=[CAP_LEAD_WRITE])
        result = self._result(self._call(token, "list_crm_leads"))
        self.assertTrue(result["isError"])

    def test_detail_excludes_internal_and_sensitive_attributes(self):
        AttributeDefinition.objects.create(organization=self.organization, key="budget", name="Budget", description="Customer stated budget", field_type="text")
        AttributeDefinition.objects.create(organization=self.organization, key="api_secret", name="API Secret", field_type="text")
        self.lead.attributes = {"budget": "5000", "api_secret": "private", "flow_state": {"private": True}}
        self.lead.save(update_fields=["attributes"])
        result = self._result(self._call(self.token(), "get_crm_lead", {"lead_id": str(self.lead.id)}))
        self.assertFalse(result["isError"], result)
        attributes = result["structuredContent"]["lead"]["attributes"]
        self.assertEqual(attributes["budget"], "5000")
        self.assertNotIn("api_secret", attributes)
        self.assertNotIn("flow_state", attributes)
        self.assertIn("booked_at", attributes)

    def test_create_dry_run_has_no_rows_and_apply_has_no_welcome_or_workflow(self):
        token = self.token()
        args = self.create_arguments()
        count = Lead.objects.filter(organization=self.organization).count()
        dry = self._result(self._call(token, "create_crm_lead", args))
        self.assertFalse(dry["isError"], dry)
        self.assertEqual(Lead.objects.filter(organization=self.organization).count(), count)
        with patch("services.crm.lead_service._schedule_new_lead_welcome") as welcome:
            result = self._result(self._call(token, "create_crm_lead", {**args, "dry_run": False, "approved": True,
                "approval_event_id": dry["structuredContent"]["approval_event_id"]}))
        self.assertFalse(result["isError"], result)
        welcome.assert_not_called()
        created = Lead.objects.get(pk=result["structuredContent"]["lead"]["id"])
        self.assertFalse(created.ai_enabled)
        self.assertFalse(created.auto_followup_enabled)
        self.assertFalse(TriggerEvent.objects.filter(lead=created).exists())
        self.assertTrue(LeadActivity.objects.filter(lead=created, topic=LeadActivity.Topic.LEAD_CREATED).exists())

    def test_explicit_workflow_opt_in_emits_only_after_apply(self):
        result = self.approve(self.token(), "create_crm_lead", self.create_arguments(allow_workflows=True))
        self.assertFalse(result["isError"], result)
        self.assertTrue(TriggerEvent.objects.filter(lead_id=result["structuredContent"]["lead"]["id"], kind="lead_created").exists())

    def test_create_rejects_cross_tenant_stage_qualified_and_unknown_fields(self):
        token = self.token()
        for args in (
            self.create_arguments(stage_id=str(self.other_lead.stage_id)),
            self.create_arguments(stage_id=str(self.qualified.id)),
            self.create_arguments(data={"name": "Forbidden", "phone": "+919888777666", "organization_id": str(self.other_organization.id)}),
        ):
            self.assertTrue(self._result(self._call(token, "create_crm_lead", args))["isError"])
        self.assertEqual(Lead.objects.filter(organization=self.organization).count(), 1)

    def test_create_cannot_execute_without_exact_approval(self):
        token = self.token()
        args = self.create_arguments()
        missing = self._result(self._call(token, "create_crm_lead", {**args, "dry_run": False}))
        self.assertTrue(missing["isError"])
        dry = self._result(self._call(token, "create_crm_lead", args))
        changed = self._result(self._call(token, "create_crm_lead", {**args, "allow_workflows": True,
            "dry_run": False, "approved": True, "approval_event_id": dry["structuredContent"]["approval_event_id"]}))
        self.assertTrue(changed["isError"])
        self.assertFalse(Lead.objects.filter(phone=args["data"]["phone"]).exists())

    def test_phone_less_instagram_retry_does_not_duplicate(self):
        token = self.token()
        args = self.create_arguments(data={"name": "Instagram Contact", "phone": "", "lead_source": "instagram"})
        result = self.approve(token, "create_crm_lead", args)
        self.assertFalse(result["isError"], result)
        repeat = self._result(self._call(token, "create_crm_lead", args))
        self.assertTrue(repeat["isError"])
        self.assertEqual(Lead.objects.filter(organization=self.organization, name="Instagram Contact").count(), 1)

    def test_update_validates_attributes_preserves_internal_state_and_records_history(self):
        AttributeDefinition.objects.create(organization=self.organization, key="budget", name="Budget", field_type="numeric")
        self.lead.attributes = {"flow_state": {"question": "Q2"}}
        self.lead.save(update_fields=["attributes"])
        result = self.approve(self.token(), "update_crm_lead", {"lead_id": str(self.lead.id),
            "changes": {"name": "Updated Aarav", "attributes": {"budget": "1200"}}, "reason": "Save the customer's corrected lead details"})
        self.assertFalse(result["isError"], result)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.name, "Updated Aarav")
        self.assertEqual(self.lead.attributes["flow_state"], {"question": "Q2"})
        self.assertEqual(self.lead.attributes["budget"], "1200")
        self.assertTrue(LeadActivity.objects.filter(lead=self.lead, topic=LeadActivity.Topic.LEAD_UPDATED).exists())

    def test_update_refuses_attribute_type_errors_and_routing_fields(self):
        AttributeDefinition.objects.create(organization=self.organization, key="budget", name="Budget", field_type="numeric")
        token = self.token()
        for changes in ({"attributes": {"budget": "not-a-number"}}, {"stage_id": str(self.qualified.id)}, {"attributes": {"flow_state": "hijack"}}, {"attributes": {"booked_at": "2030-01-01T10:00"}}):
            result = self._result(self._call(token, "update_crm_lead", {"lead_id": str(self.lead.id), "changes": changes, "reason": "Update lead configuration for review"}))
            self.assertTrue(result["isError"])

    def test_update_approval_detects_intervening_human_edit(self):
        token = self.token()
        args = {"lead_id": str(self.lead.id), "changes": {"name": "Agent edit"}, "reason": "Correct the lead contact name"}
        dry = self._result(self._call(token, "update_crm_lead", args))
        self.assertFalse(dry["isError"], dry)
        self.lead.name = "Human edit"
        self.lead.save(update_fields=["name", "updated_at"])
        result = self._result(self._call(token, "update_crm_lead", {**args, "dry_run": False, "approved": True,
            "approval_event_id": dry["structuredContent"]["approval_event_id"]}))
        self.assertTrue(result["isError"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.name, "Human edit")

    def test_import_skips_normalized_duplicates_and_never_overwrites(self):
        args = self.create_arguments()
        args.pop("data")
        args.update(rows=[{"name": "Do not overwrite", "phone": "+91 99999 99991"},
                          {"name": "Imported", "phone": "+919877665544"}], duplicate_policy="skip")
        result = self.approve(self.token(), "import_crm_leads", args)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["created_count"], 1)
        self.assertEqual(result["structuredContent"]["skipped_rows"], [1])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.name, "Aarav")
        created = Lead.objects.get(phone="+919877665544")
        self.assertEqual(created.lead_source, "csv_import")
        self.assertFalse(TriggerEvent.objects.filter(lead=created).exists())

    def test_import_revalidates_before_any_write(self):
        token = self.token()
        args = self.create_arguments()
        args.pop("data")
        args["rows"] = [{"name": "First", "phone": "+919877665544"}, {"name": "Bad", "phone": "123"}]
        result = self._result(self._call(token, "import_crm_leads", args))
        self.assertTrue(result["isError"])
        self.assertFalse(Lead.objects.filter(phone="+919877665544").exists())

    def test_bulk_move_all_or_nothing_tenant_and_qualification_validation(self):
        token = self.token()
        for ids, target in (([str(self.lead.id), str(self.other_lead.id)], self.review_stage),
                            ([str(self.lead.id)], self.qualified)):
            result = self._result(self._call(token, "bulk_move_crm_leads", {"lead_ids": ids,
                "target_stage_id": str(target.id), "reason": "Move selected leads to reviewed stage"}))
            self.assertTrue(result["isError"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_stage.id)

    def test_bulk_move_uses_canonical_transition_and_history(self):
        result = self.approve(self.token(), "bulk_move_crm_leads", {"lead_ids": [str(self.lead.id)],
            "target_stage_id": str(self.review_stage.id), "reason": "Move selected leads into manual review"})
        self.assertFalse(result["isError"], result)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.review_stage.id)
        self.assertTrue(LeadActivity.objects.filter(lead=self.lead, topic=LeadActivity.Topic.STAGE_CHANGED).exists())

    def test_superadmin_provisions_default_crm_and_inactive_owner_without_messages(self):
        token = self._token(actor=self.superadmin, role=ROLE_SUPERADMIN)
        args = {"data": {"name": "Provisioned Customer", "owner": {"name": "Owner", "email": "new-owner@example.test", "phone": "9877665544"}},
                "reason": "Provision the newly approved customer workspace"}
        with self.captureOnCommitCallbacks(execute=True), patch("django.core.mail.send_mail") as mail:
            result = self.approve(token, "create_organization_account", args)
        self.assertFalse(result["isError"], result)
        mail.assert_not_called()
        organization = Organization.objects.get(pk=result["structuredContent"]["organization_id"])
        self.assertEqual(organization.pipelines.get(name="Leads").stages.count(), 8)
        owner = User.objects.get(organization=organization)
        self.assertFalse(owner.is_active)
        self.assertFalse(owner.has_usable_password())
        self.assertEqual(owner.role, User.Role.ADMIN)
        self.assertEqual(owner.phone, "+919877665544")
        audit = OperationsAuditEvent.objects.filter(tool_name="create_organization_account", outcome=OperationsAuditEvent.Outcome.SUCCESS).latest("created_at")
        self.assertIsNone(audit.organization_id)

    def test_org_admin_cannot_provision_another_organization_even_if_policy_lists_capability(self):
        policy = OperationsPolicy.objects.get(organization=self.organization)
        policy.allowed_capabilities.append(CAP_ORGANIZATION_CREATE)
        policy.save()
        token = self.token()
        result = self._result(self._call(token, "create_organization_account", {
            "data": {"name": "Unauthorized Tenant"}, "reason": "Provision another organization account"}))
        self.assertTrue(result["isError"])
        self.assertFalse(Organization.objects.filter(name="Unauthorized Tenant").exists())

    def test_workflow_suppression_restores_after_nested_exception(self):
        with self.assertRaises(RuntimeError):
            with suppress_workflow_events():
                with suppress_workflow_events():
                    self.assertIsNone(emit(self.lead, "lead_created", "suppressed"))
                raise RuntimeError("fixture")
        self.assertIsNotNone(emit(self.lead, "lead_created", "restored"))
