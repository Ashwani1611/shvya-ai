from types import SimpleNamespace

from django.test import TestCase

from apps.crm.models.attribute import AttributeDefinition
from apps.organizations.models import Organization
from services.content_authoring import (
    ContentAuthoringError,
    normalize_plain_text,
    render_personalized_text,
    supported_placeholder_keys,
)


class ContentAuthoringTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Plain Text Org")
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Industry",
            key="industry",
            field_type=AttributeDefinition.FieldType.TEXT,
        )

    def test_plain_text_normalization_preserves_supported_placeholders(self):
        value = (
            "<b>Hello</b> **{lead_first_name}**\n"
            "_Welcome_ to [our site](https://example.com)."
        )
        normalized = normalize_plain_text(
            value,
            organization=self.organization,
            field="Message",
            allow_placeholders=True,
            required=True,
        )
        self.assertEqual(
            normalized,
            "Hello {{lead_first_name}}\nWelcome to our site (https://example.com).",
        )

    def test_custom_tenant_placeholder_is_supported_and_canonicalized(self):
        self.assertIn(
            "industry",
            supported_placeholder_keys(organization=self.organization),
        )
        normalized = normalize_plain_text(
            "Built for {industry} teams.",
            organization=self.organization,
            field="Message",
            allow_placeholders=True,
        )
        self.assertEqual(normalized, "Built for {{industry}} teams.")

    def test_unknown_placeholder_is_rejected(self):
        with self.assertRaisesRegex(
            ContentAuthoringError,
            "unsupported placeholder",
        ):
            normalize_plain_text(
                "Hello {{not_real}}",
                organization=self.organization,
                field="Message",
                allow_placeholders=True,
            )

    def test_static_title_rejects_placeholders(self):
        with self.assertRaisesRegex(
            ContentAuthoringError,
            "does not support placeholders",
        ):
            normalize_plain_text(
                "Welcome {{lead_name}}",
                organization=self.organization,
                field="Title",
                allow_placeholders=False,
            )

    def test_plain_text_normalization_does_not_damage_underscored_words(self):
        normalized = normalize_plain_text(
            "Keep lead_first_name as ordinary text.",
            organization=self.organization,
            field="Message",
            allow_placeholders=True,
        )
        self.assertEqual(
            normalized,
            "Keep lead_first_name as ordinary text.",
        )

    def test_personalized_render_uses_builtin_and_custom_values(self):
        lead = SimpleNamespace(
            name="Jane Doe",
            phone="+919999999999",
            email="jane@example.com",
            lead_source="Website",
            organization=self.organization,
            pipeline_id=None,
            stage_id=None,
            attributes={"industry": "Hospitality"},
        )
        rendered = render_personalized_text(
            "Hi {{lead_first_name}}, {{industry}} is noted.",
            lead=lead,
        )
        self.assertEqual(
            rendered,
            "Hi Jane, Hospitality is noted.",
        )
