from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, SimpleTestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import Lead, Stage, AttributeDefinition
from apps.organizations.models import Organization
from services.crm.stage_requirements import has_value, clean_entry_values
from services.crm.lead_transition import move_lead_to_stage


class EntryValueTests(SimpleTestCase):
    def test_zero_and_false_are_present_but_whitespace_is_not(self):
        for value in (0, False, "0", "hello"):
            self.assertTrue(has_value(value))
        for value in (None, "", "  ", [], {}):
            self.assertFalse(has_value(value))

    def test_invalid_numeric_value_is_rejected(self):
        attribute = AttributeDefinition(
            key="budget", name="Budget", field_type="numeric"
        )
        with patch(
            "services.crm.stage_requirements.required_attributes",
            return_value=[attribute],
        ):
            values, errors = clean_entry_values(None, {}, {"attr_budget": "NaN"})
            self.assertTrue(errors)
            self.assertNotIn("budget", values)


class StageEditorTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Stage editor test")
        self.user = User.objects.create_user(
            email="editor@example.com",
            organization=self.org,
            password="test",
            role=User.Role.ADMIN,
            name="Editor",
        )
        self.pipeline = self.org.pipelines.get(is_active=True)
        self.source = self.pipeline.stages.first()
        self.target = Stage.objects.create(
            pipeline=self.pipeline, name="Review", display_order=100
        )
        self.attribute = AttributeDefinition.objects.create(
            organization=self.org, name="Reason", key="reason"
        )
        self.target.config = {
            "required_attribute_ids": [str(self.attribute.id)],
            "other": True,
        }
        self.target.save()
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.source,
            name="Maya",
            phone="+919876543210",
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def url(self, name, **kwargs):
        return reverse(name, kwargs=kwargs)

    def test_manual_move_prompts_without_mutation_then_moves(self):
        url = self.url("crm-lead-stage-move", lead_id=self.lead.id)
        response = self.client.post(url, {"stage": str(self.target.id)})
        self.assertContains(response, "One more step.")
        self.assertEqual(response["HX-Retarget"], "#modal-root")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.source.id)
        response = self.client.post(
            url, {"stage": str(self.target.id), "attr_reason": "  "}
        )
        self.assertContains(response, "One more step.")
        response = self.client.post(
            url, {"stage": str(self.target.id), "attr_reason": "Good fit"}
        )
        self.assertEqual(response.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.target.id)
        self.assertEqual(self.lead.attributes["reason"], "Good fit")

    def test_automation_service_skips_requirement(self):
        move_lead_to_stage(lead=self.lead, stage=self.target)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.target.id)
        self.assertNotIn("reason", self.lead.attributes)

    def test_cannot_bypass_manual_check_with_move_type(self):
        response = self.client.post(
            self.url("crm-lead-stage-move", lead_id=self.lead.id),
            {"stage": str(self.target.id), "move_type": "bulk"},
        )
        self.assertContains(response, "One more step.")

    def test_requirements_validate_organization_and_preserve_config(self):
        url = self.url("crm-stage-editor-requirements", stage_id=self.target.id)
        self.assertEqual(
            self.client.post(url, {"attributes": ["invalid"]}).status_code, 400
        )
        self.assertEqual(self.client.post(url, {}).status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(
            self.target.config, {"required_attribute_ids": [], "other": True}
        )

    def test_order_swaps_without_unique_constraint_collision(self):
        previous = list(
            self.pipeline.stages.filter(is_active=True).order_by("display_order")
        )[-2]
        old = previous.display_order
        response = self.client.post(
            self.url("crm-stage-editor-reorder", stage_id=self.target.id),
            {"direction": "up"},
        )
        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        previous.refresh_from_db()
        self.assertEqual(self.target.display_order, old)
        self.assertEqual(previous.display_order, 100)

    def test_ai_is_separate_from_text(self):
        response = self.client.post(
            self.url("crm-stage-editor-ai-toggle", stage_id=self.target.id),
            {"enabled": "false", "name": "Do not save"},
        )
        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.ai_on)
        self.assertEqual(self.target.name, "Review")

    def test_modal_renders_requirements(self):
        response = self.client.get(
            reverse("crm-stage-editor-modal"), {"pipeline": self.pipeline.id}
        )
        self.assertContains(response, "Required before entering")
        self.assertContains(response, "Save changes")
        self.assertNotContains(response, "Saved automatically")

    def test_bulk_move_skips_required_attributes(self):
        response = self.client.post(
            reverse("crm-leads-bulk"),
            {
                "action": "update",
                "pipeline": str(self.pipeline.id),
                "source_stage": str(self.source.id),
                "lead_ids": [str(self.lead.id)],
                "move": True,
                "target_pipeline": str(self.pipeline.id),
                "target_stage": str(self.target.id),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.target.id)

    def test_edit_lead_cannot_skip_required_attributes(self):
        response = self.client.post(
            reverse("crm-lead-edit-save", kwargs={"lead_id": self.lead.id}),
            {
                "pipeline": str(self.pipeline.id),
                "stage": str(self.target.id),
                "name": "Updated Maya",
            },
        )
        self.assertContains(response, "One more step.")
        self.assertContains(response, "Updated Maya")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.name, "Maya")
        self.assertEqual(self.lead.stage_id, self.source.id)

    def test_inactive_stage_requirement_is_not_reachable(self):
        self.target.is_active = False
        self.target.save()
        response = self.client.post(
            reverse("crm-lead-stage-move", kwargs={"lead_id": self.lead.id}),
            {"stage": str(self.target.id), "attr_reason": "Ready"},
        )
        self.assertEqual(response.status_code, 400)
