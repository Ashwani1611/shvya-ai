"""Configured languages survive finite replies when model generation fails."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.engagement_failsoft import _ensure_customer_reply, _grounded_conversation_reply
from apps.ai_engagement.services.response_fallbacks import fallback_language, fallback_message, is_technical_fallback


class ResponseFallbackLanguageTests(SimpleTestCase):
    def test_every_failure_kind_is_available_in_each_added_language(self):
        scripts = {"Punjabi": r"[\u0a00-\u0a7f]", "Marathi": r"[\u0900-\u097f]",
                   "Kannada": r"[\u0c80-\u0cff]", "German": r"[A-Za-zäöüß]"}
        kinds = ("technical", "unverified", "pricing", "policy", "ambiguous", "conflicting", "action", "qualification")
        for language, script in scripts.items():
            outputs = []
            for kind in kinds:
                with self.subTest(language=language, kind=kind):
                    text = fallback_message(kind=kind, bot_languages=language, latest_text="PRIVATE_CUSTOMER_TEXT")
                    self.assertRegex(text, script)
                    self.assertNotEqual(text, fallback_message(kind=kind, bot_languages="English"))
                    self.assertNotIn("PRIVATE_CUSTOMER_TEXT", text)
                    outputs.append(text)
            self.assertEqual(len(set(outputs)), len(kinds))

    def test_configured_aliases_and_script_hints_never_enable_another_language(self):
        cases = (
            ("ਪੰਜਾਬੀ", "Price?", "punjabi"), ("pa_IN", "Price?", "punjabi"),
            ("मराठी", "Price?", "marathi"), ("mr", "Price?", "marathi"),
            ("Deutsch", "Price?", "german"), ("de-DE", "Price?", "german"),
            ("ಕನ್ನಡ", "Price?", "kannada"), ("kn-IN", "Price?", "kannada"),
            ("English, Punjabi", "ਕੀਮਤ ਕੀ ਹੈ?", "punjabi"),
            ("English, Kannada", "ಬೆಲೆ ಎಷ್ಟು?", "kannada"),
            ("English, Marathi", "किंमत किती आहे?", "marathi"),
            ("English, Hindi", "कीमत क्या है?", "hindi"),
            ("English, Marathi, Hindi", "किंमत किती आहे?", "marathi"),
            ("English, Hindi, Marathi", "किंमत किती आहे?", "hindi"),
            ("German, English", "What is the price?", "german"),
            ("English, German", "Was kostet das?", "english"),
            ("English", "ਕੀਮਤ ਕੀ ਹੈ? ಬೆಲೆ ಎಷ್ಟು? किंमत किती?", "english"),
            ("German", "कीमत क्या है?", "german"),
            ("Hinglish", "कीमत क्या है?", "hinglish"),
            ("हिंदी", "Price?", "hindi"),
            ("", "कीमत क्या है?", "hindi"), ("", "Price?", "english"),
        )
        for configured, incoming, expected in cases:
            with self.subTest(configured=configured, incoming=incoming):
                self.assertEqual(fallback_language(configured, incoming), expected)

    def test_recovery_and_history_recognition_use_localized_technical_message(self):
        for language in ("Punjabi", "Marathi", "German", "Kannada"):
            with self.subTest(language=language):
                technical = fallback_message(bot_languages=language)
                self.assertTrue(is_technical_fallback(technical))
                self.assertEqual(fallback_message(kind="unknown-kind", bot_languages=language), technical)
                self.assertEqual(fallback_message(kind="unverified", question_type="pricing", bot_languages=language),
                                 fallback_message(kind="pricing", bot_languages=language))

    def test_grounded_reply_helper_passes_configured_language_to_missing_price(self):
        for language in ("Punjabi", "Marathi", "German", "Kannada"):
            with self.subTest(language=language):
                message, reason = _grounded_conversation_reply(
                    about="", inbound="What is the price?", organization_name="", bot_languages=language,
                )
                self.assertEqual(message, fallback_message(kind="pricing", bot_languages=language))
                self.assertEqual(reason, "UNKNOWN_INFORMATION")

    def test_terminal_reply_guard_keeps_organization_language_without_provider_calls(self):
        organization = SimpleNamespace(id="org", pk="org")
        lead = SimpleNamespace(id="lead", organization=organization, attributes={})
        source = SimpleNamespace(id="source", body="What is the price?", raw_payload={})
        decision = EngagementDecision(should_engage=False, message="", file_document_id=None,
                                      crm_actions=[], reason="MODEL_SILENCE", model="test")
        for language in ("Punjabi", "Marathi", "German", "Kannada"):
            with self.subTest(language=language), \
                 patch("apps.ai_engagement.services.engagement_failsoft._latest_inbound_for_lead", return_value=source), \
                 patch("apps.ai_engagement.models.OrgInfo.objects.filter") as query:
                query.return_value.only.return_value.first.return_value = SimpleNamespace(bot_languages=language)
                result = _ensure_customer_reply(decision, lead=lead)
                self.assertEqual(result.message, fallback_message(bot_languages=language))
                self.assertTrue(result.should_engage)
                self.assertEqual(result.crm_actions, [])
                self.assertEqual(result.qualification_updates, [])
                self.assertIsNone(result.file_document_id)

    def test_terminal_reply_guard_survives_language_storage_failure(self):
        lead = SimpleNamespace(id="lead", organization=SimpleNamespace(id="org", pk="org"), attributes={})
        source = SimpleNamespace(id="source", body="What is the price?", raw_payload={})
        decision = EngagementDecision(should_engage=False, message="", file_document_id=None,
                                      crm_actions=[], reason="MODEL_SILENCE", model="test")
        with patch("apps.ai_engagement.services.engagement_failsoft._latest_inbound_for_lead", return_value=source), \
             patch("apps.ai_engagement.models.OrgInfo.objects.filter", side_effect=RuntimeError("unavailable")):
            result = _ensure_customer_reply(decision, lead=lead)
        self.assertEqual(result.message, fallback_message(bot_languages="English"))
        self.assertTrue(result.should_engage)
