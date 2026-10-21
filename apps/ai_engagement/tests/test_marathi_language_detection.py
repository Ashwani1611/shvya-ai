"""Marathi qualification statements must not fall back to English."""
from unittest import TestCase

from apps.ai_engagement.services.intent_rules import canonical_language, detect_language


class MarathiQualificationLanguageTests(TestCase):
    def test_natural_marathi_qualification_and_explicit_request(self):
        for body in ("आम्ही लीड्स Excel मध्ये ठेवतो.", "आमची मुख्य समस्या म्हणजे लीड्सना उशिरा उत्तर देणे.", "नमस्कार! मराठीत उत्तर द्या.", "माझा व्यवसाय छोटा आहे."):
            with self.subTest(body=body):
                self.assertEqual(canonical_language(detect_language(body)), "marathi")

    def test_hindi_and_english_are_not_reclassified_as_marathi(self):
        for body, expected in (("हम लीड्स Excel में रखते हैं।", "hindi"), ("We keep leads in Excel.", "english")):
            with self.subTest(body=body):
                self.assertEqual(canonical_language(detect_language(body)), expected)
