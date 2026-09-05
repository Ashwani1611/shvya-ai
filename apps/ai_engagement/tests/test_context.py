from __future__ import annotations

from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.context import AIContextBuilder
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class AIContextBuilderTests(TestCase):
    """
    Tests the centralized runtime AI context builder.

    These tests verify that:

        Organization
            +
        OrgInfo
            +
        Lead
            +
        Pipeline
            +
        Stage

    are assembled into one AIContext.

    The builder must remain a context-only service.

    It must NOT:
        - generate AI output
        - send messages
        - modify CRM
        - create notes
        - modify summaries
        - perform qualification
        - perform bump-up decisions
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="AI Context Test Organization",
        )

        cls.org_info = OrgInfo.objects.create(
            organization=cls.organization,
            about=(
                "Cybersecurity training academy "
                "providing certification programs."
            ),
            bot_languages="English, Hindi, Hinglish",
            qualification_requirements=(
                "Identify course interest, budget, "
                "timeline, and buying intent."
            ),
            engagement_instructions=(
                "Use a friendly, concise, and helpful tone "
                "when speaking with leads."
            ),
            ai_enabled=True,
            bump_up_enabled=True,
            bump_up_count=2,
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="AI Context Test Pipeline",
            description="Pipeline used for AI context tests.",
            is_active=True,
        )

        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New Lead",
            description="New incoming lead.",
            display_order=0,
            is_active=True,
        )

        cls.lead = Lead.objects.create(
            organization=cls.organization,
            pipeline=cls.pipeline,
            stage=cls.stage,
            name="Test Lead",
            phone="+919876543210",
            email="lead@example.com",
            notes="Existing lead note.",
            attributes={
                "budget": "50000",
                "course_interest": "Cybersecurity",
            },
            lead_source="whatsapp_api",
        )

    # ========================================================
    # ORGANIZATION CONTEXT
    # ========================================================

    def test_context_includes_organization_configuration(self):
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
        )

        self.assertEqual(
            context.organization["id"],
            str(self.organization.id),
        )

        self.assertEqual(
            context.organization["name"],
            self.organization.name,
        )

        self.assertTrue(
            context.organization["ai_enabled"],
        )

        self.assertEqual(
            context.organization["about"],
            self.org_info.about,
        )

        self.assertEqual(
            context.organization["bot_languages"],
            self.org_info.bot_languages,
        )

        self.assertEqual(
            context.organization["qualification_requirements"],
            self.org_info.qualification_requirements,
        )

        self.assertEqual(
            context.organization["engagement_instructions"],
            self.org_info.engagement_instructions,
        )

        self.assertTrue(
            context.organization["bump_up_enabled"],
        )

        self.assertEqual(
            context.organization["bump_up_count"],
            2,
        )

    # ========================================================
    # MISSING ORG INFO
    # ========================================================

    def test_context_disables_ai_when_org_info_is_missing(self):
        organization = Organization.objects.create(
            name="Organization Without AI Info",
        )

        pipeline = Pipeline.objects.create(
            organization=organization,
            name="Pipeline Without AI Info",
            is_active=True,
        )

        stage = Stage.objects.create(
            pipeline=pipeline,
            name="New Lead",
            description="New lead stage.",
            display_order=0,
            is_active=True,
        )

        lead = Lead.objects.create(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
            name="Lead Without AI Info",
            phone="+919876543211",
            lead_source="whatsapp_api",
        )

        context = AIContextBuilder().build(
            organization=organization,
            lead=lead,
        )

        self.assertEqual(
            context.organization["id"],
            str(organization.id),
        )

        self.assertFalse(
            context.organization["ai_enabled"],
        )

        self.assertEqual(
            context.organization["about"],
            "",
        )

        self.assertEqual(
            context.organization["bot_languages"],
            "",
        )

        self.assertEqual(
            context.organization["qualification_requirements"],
            "",
        )

        self.assertEqual(
            context.organization["engagement_instructions"],
            "",
        )

        self.assertFalse(
            context.organization["bump_up_enabled"],
        )

        self.assertEqual(
            context.organization["bump_up_count"],
            0,
        )

    # ========================================================
    # LEAD CONTEXT
    # ========================================================

    def test_context_includes_lead_context(self):
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
        )

        self.assertEqual(
            context.lead["id"],
            str(self.lead.id),
        )

        self.assertEqual(
            context.lead["name"],
            self.lead.name,
        )

        self.assertEqual(
            context.lead["phone"],
            self.lead.phone,
        )

        self.assertEqual(
            context.lead["email"],
            self.lead.email,
        )

        self.assertEqual(
            context.lead["notes"],
            self.lead.notes,
        )

        self.assertEqual(
            context.lead["attributes"],
            self.lead.attributes,
        )

        self.assertEqual(
            context.lead["lead_source"],
            self.lead.lead_source,
        )

    # ========================================================
    # PIPELINE + STAGE CONTEXT
    # ========================================================

    def test_context_includes_pipeline_and_stage(self):
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
        )

        self.assertEqual(
            context.pipeline["id"],
            str(self.pipeline.id),
        )

        self.assertEqual(
            context.pipeline["name"],
            self.pipeline.name,
        )

        self.assertEqual(
            context.pipeline["description"],
            self.pipeline.description,
        )

        self.assertTrue(
            context.pipeline["is_active"],
        )

        self.assertEqual(
            context.stage["id"],
            str(self.stage.id),
        )

        self.assertEqual(
            context.stage["name"],
            self.stage.name,
        )

        self.assertEqual(
            context.stage["description"],
            self.stage.description,
        )

        self.assertEqual(
            context.stage["display_order"],
            self.stage.display_order,
        )

        self.assertTrue(
            context.stage["is_active"],
        )

    # ========================================================
    # ATTRIBUTES
    # ========================================================

    def test_context_normalizes_lead_attributes(self):
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
        )

        self.assertEqual(
            context.attributes,
            [
                {
                    "name": "budget",
                    "value": "50000",
                },
                {
                    "name": "course_interest",
                    "value": "Cybersecurity",
                },
            ],
        )

    # ========================================================
    # IMMUTABLE AI CONTEXT
    # ========================================================

    def test_context_is_immutable(self):
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
        )

        with self.assertRaises(
            AttributeError,
        ):
            context.organization = {}

    # ========================================================
    # AS DICT
    # ========================================================

    def test_context_can_be_serialized_to_dict(self):
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
        )

        data = context.as_dict()

        self.assertEqual(
            set(data.keys()),
            {
                "organization",
                "lead",
                "pipeline",
                "stage",
                "contacts",
                "attributes",
                "conversation",
                "conversation_summary",
                "qualification_notes",
                "knowledge",
            },
        )

        self.assertEqual(
            data["organization"]["engagement_instructions"],
            self.org_info.engagement_instructions,
        )

        self.assertEqual(
            data["lead"]["id"],
            str(self.lead.id),
        )

        self.assertEqual(
            data["pipeline"]["id"],
            str(self.pipeline.id),
        )

        self.assertEqual(
            data["stage"]["id"],
            str(self.stage.id),
        )

    # ========================================================
    # ORGANIZATION ISOLATION
    # ========================================================

    def test_context_rejects_lead_from_another_organization(self):
        other_organization = Organization.objects.create(
            name="Other Organization",
        )

        other_pipeline = Pipeline.objects.create(
            organization=other_organization,
            name="Other Pipeline",
            is_active=True,
        )

        other_stage = Stage.objects.create(
            pipeline=other_pipeline,
            name="Other Stage",
            description="Other stage.",
            display_order=0,
            is_active=True,
        )

        other_lead = Lead.objects.create(
            organization=other_organization,
            pipeline=other_pipeline,
            stage=other_stage,
            name="Other Lead",
            phone="+919876543212",
            lead_source="whatsapp_api",
        )

        with self.assertRaisesRegex(
            Exception,
            "does not belong to this organization",
        ):
            AIContextBuilder().build(
                organization=self.organization,
                lead=other_lead,
            )