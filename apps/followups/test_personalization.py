from django.test import TestCase

from apps.crm.models import Lead, Pipeline, Stage
from apps.followups.templatetags.followup_tags import followup_placeholders
from apps.organizations.models import Organization


class FollowupPersonalizationPlaceholderTests(TestCase):
    def test_core_and_real_attribute_placeholders_are_exposed(self):
        organization = Organization.objects.create(name="Personalization Org")
        pipeline = Pipeline.objects.create(organization=organization, name="Sales")
        stage = Stage.objects.get(pipeline=pipeline, display_order=1)
        Lead.objects.create(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
            name="Asha Mehta",
            phone="+919111111111",
            email="asha@example.com",
            attributes={"company_name": "Acme", "city": "Delhi"},
        )

        tokens = {item["token"] for item in followup_placeholders(organization)}

        self.assertTrue(
            {
                "{{lead_name}}",
                "{{lead_first_name}}",
                "{{phone}}",
                "{{email}}",
                "{{org_name}}",
                "{{company_name}}",
                "{{city}}",
            }.issubset(tokens)
        )
