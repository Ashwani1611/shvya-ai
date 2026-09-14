from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.attribute_state_reconciliation import (
    reconcile_lead_qualification_from_attributes,
)
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import state_for_lead
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests
from apps.crm.models import AttributeDefinition


class ExistingAttributeQualificationTests(TestCase):
    setUp = AIEngagementControlTests.setUp

    def test_existing_attribute_description_satisfies_requirement_without_reasking(self):
        org_info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        org_info.qualification_requirements = (
            "[id: management] Where do you currently manage leads?\n"
            "A. WhatsApp chats\n"
            "B. Excel / Sheets\n"
            "C. CRM\n"
            "D. Multiple places"
        )
        org_info.engagement_instructions = "Ask only unanswered qualification questions."
        org_info.ai_enabled = True
        org_info.save()
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Lead Management Tool",
            key="lead_management_tool",
            description=(
                "Where the lead currently manages leads. This stores the answer "
                "to the lead-management qualification requirement."
            ),
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        attributes = dict(self.lead.attributes or {})
        attributes["lead_management_tool"] = "CRM"
        self.lead.attributes = attributes
        self.lead.save(update_fields=["attributes", "updated_at"])

        state = reconcile_lead_qualification_from_attributes(
            organization=self.organization,
            lead=self.lead,
        )
        requirements = compile_qualification_requirements(
            org_info.qualification_requirements
        )["requirements"]
        requirement_id = requirements[0]["id"]
        self.assertEqual(
            state["requirement_states"][requirement_id]["status"],
            "answered",
        )
        self.assertEqual(
            state["requirement_states"][requirement_id]["value"],
            "CRM",
        )
        self.assertIsNone(state["next_requirement_id"])
        self.assertEqual(state["qualification_status"], "completed")

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        stable = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(stable["qualification_status"], "completed")

    def test_ambiguous_attribute_mapping_is_not_used(self):
        org_info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        org_info.qualification_requirements = "What is your requirement?"
        org_info.ai_enabled = True
        org_info.save()
        for index in (1, 2):
            AttributeDefinition.objects.create(
                organization=self.organization,
                name=f"Requirement {index}",
                key=f"requirement_{index}",
                description="General requirement information supplied by the lead.",
                field_type=AttributeDefinition.FieldType.TEXT,
            )
        attributes = dict(self.lead.attributes or {})
        attributes["requirement_1"] = "Value one"
        attributes["requirement_2"] = "Value two"
        self.lead.attributes = attributes
        self.lead.save(update_fields=["attributes", "updated_at"])

        state = reconcile_lead_qualification_from_attributes(
            organization=self.organization,
            lead=self.lead,
        )
        requirements = compile_qualification_requirements(
            org_info.qualification_requirements
        )["requirements"]
        requirement_id = requirements[0]["id"]
        self.assertNotEqual(
            state["requirement_states"][requirement_id]["status"],
            "answered",
        )
        self.assertEqual(self.lead.stage_id, self.new_lead.id)
