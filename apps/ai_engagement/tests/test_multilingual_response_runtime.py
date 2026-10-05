"""Language selection regressions for configured AI Brain languages."""
from django.test import SimpleTestCase

from apps.ai_engagement.services.intent_rules import detect_language
from apps.ai_engagement.services.response_fallbacks import fallback_language, fallback_message


class MultilingualResponseRuntimeTests(SimpleTestCase):
    def test_detection_covers_configured_scripts_and_roman_hinglish(self):
        cases = (
            ("ਤੁਹਾਡੇ ਪਲਾਨ ਦੀ ਕੀਮਤ ਕਿੰਨੀ ਹੈ?", "pa"),
            ("तुमच्या प्लॅनची किंमत किती आहे?", "mr"),
            ("ನಿಮ್ಮ ಪ್ಲಾನ್ ಬೆಲೆ ಎಷ್ಟು?", "kn"),
            ("Aapka plan kitne ka hai?", "hinglish"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(detect_language(text), expected)

    def test_english_is_not_misclassified_as_german(self):
        self.assertEqual(detect_language("What is the price of the plan?"), "en")
        self.assertEqual(detect_language("Was kostet der Plan?"), "de")

    def test_fallback_uses_the_customer_language_when_configured(self):
        cases = (
            ("English, Punjabi", "ਕੀਮਤ ਕੀ ਹੈ?", "punjabi"),
            ("English, Marathi", "किंमत किती आहे?", "marathi"),
            ("English, German", "Was kostet das?", "german"),
            ("English, Kannada", "ಬೆಲೆ ಎಷ್ಟು?", "kannada"),
        )
        for configured, inbound, expected in cases:
            with self.subTest(configured=configured):
                self.assertEqual(fallback_language(configured, inbound), expected)
                self.assertNotEqual(fallback_message(bot_languages=configured, latest_text=inbound),
                                    fallback_message(bot_languages="English"))
