from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement_instruction_runtime import (
    _definition_reference_score, _mapped_attribute_key,
)
from apps.ai_engagement.services.qualification_execution.config import _mapped_value


class AuthoredMappingTargetTests(SimpleTestCase):
    def setUp(self):
        self.definitions = [
            {"key": "source", "name": "Source"},
            {"key": "biggest_problem", "name": "Biggest Problem"},
        ]
        self.rule = (
            "Mapping 1:\n- Attribute name: BIGGEST PROBLEM\n"
            "- Description: The customer's lead-management difficulty.\n"
            "- Source: Qualification Question 1 or an explicit customer statement.\n"
            "- Value rule: Slow replies → Slow replies."
        )

    def test_source_metadata_is_not_a_mapping_destination(self):
        self.assertEqual(_definition_reference_score(self.rule, self.definitions[0]), 0)
        self.assertEqual(_definition_reference_score(self.rule, self.definitions[1]), 100)
        self.assertEqual(_mapped_attribute_key(
            requirement={"id": "problem", "priority": 1},
            definitions=self.definitions, rules=[self.rule],
        ), "biggest_problem")

    def test_unknown_explicit_target_cannot_fall_back_to_source_or_description(self):
        rule = self.rule.replace("BIGGEST PROBLEM", "UNKNOWN FIELD")
        self.assertIsNone(_mapped_attribute_key(
            requirement={"id": "problem", "priority": 1},
            definitions=self.definitions, rules=[rule],
        ))

    def test_explicit_source_mapping_and_legacy_inline_mapping_still_work(self):
        self.assertEqual(_definition_reference_score(
            "Attribute name: SOURCE\nSource: explicit customer evidence.", self.definitions[0],
        ), 100)
        self.assertEqual(_definition_reference_score(
            "Map Question 1 to biggest_problem", self.definitions[1],
        ), 100)

    def test_explicit_key_accepts_underscores_and_markdown(self):
        self.assertEqual(_definition_reference_score(
            "- Attribute key: `biggest_problem`", self.definitions[1],
        ), 100)

    def test_authored_numeric_band_maps_to_configured_crm_value(self):
        config = {"mapping_value_rules": {"volume": (
            "- Attribute name: VOLUME\n- Source: Question 3\n"
            "- 0–10 leads per day → 0-10.\n"
            "- 11–30 leads per day → 10-30.\n"
            "- More than 30 leads per day → 30+."
        )}}
        self.assertEqual(_mapped_value(config, "volume", "11–30"), "10-30")
        self.assertEqual(_mapped_value(config, "volume", "11-30"), "10-30")
        self.assertEqual(_mapped_value(config, "volume", "0–10"), "0-10")
        self.assertEqual(_mapped_value(config, "volume", "11"), "11")
        self.assertEqual(_mapped_value(config, "volume", "31–40"), "31–40")
