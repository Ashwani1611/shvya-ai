"""Explicit native-language requests must retain the same time/authority gates."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.authored_reminder_rules import reminder_request_kind
from apps.ai_engagement.services.crm_routing_reliability import _ensure_datetime_reminder
from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at


NOW = datetime(2026, 10, 4, 19, 30, tzinfo=timezone.utc)
CALLBACK_RULE = (
    "Reminder 1:\n"
    "- Create when the customer explicitly requests a callback and provides or confirms a future date and time.\n"
    "- Title: Customer Callback."
)
FOLLOWUP_RULE = CALLBACK_RULE.replace("callback", "later follow-up").replace("Customer Callback", "Customer Follow-up")


class ConfiguredLanguageReminderTests(SimpleTestCase):
    def apply(self, text, *, rules=None, timezone_name="Asia/Kolkata", channel="whatsapp"):
        context = SimpleNamespace(
            organization={"timezone": timezone_name},
            conversation={"channel": channel, "messages": [{"id": "current", "direction": "inbound", "body": text}]},
        )
        actions = []
        with patch("apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=NOW):
            _ensure_datetime_reminder(actions, text, {"crm": {"reminders": rules or []}}, context=context)
        return actions

    def parse(self, text, timezone_name="Asia/Kolkata"):
        with patch("apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=NOW):
            return parse_grounded_due_at(text, timezone_name=timezone_name)

    def test_callbacks_keep_native_day_clock_digits_and_authored_title_across_channels(self):
        texts = (
            "Bitte rufen Sie mich morgen um 15 Uhr an",
            "मला उद्या दुपारी ३ वाजता फोन करा",
            "ਮੈਨੂੰ ਭਲਕੇ ਦੁਪਹਿਰੇ ੩ ਵਜੇ ਕਾਲ ਕਰੋ",
            "ನನಗೆ ನಾಳೆ ಮಧ್ಯಾಹ್ನ ೩ ಗಂಟೆಗೆ ಕರೆ ಮಾಡಿ",
        )
        for text in texts:
            for channel in ("whatsapp", "hosted", "instagram", "sandbox"):
                with self.subTest(text=text, channel=channel):
                    action = self.apply(text, rules=[CALLBACK_RULE], channel=channel)[0]
                    self.assertEqual(action["title"], "Customer Callback")
                    self.assertEqual(action["due_at"], "2026-10-06T15:00:00+05:30")
                    self.assertEqual(action["description"], text)

    def test_followup_requests_use_followup_rule(self):
        for text in (
            "Erinnere mich morgen um 15:00 Uhr",
            "मला उद्या १५:०० वाजता आठवण करून द्या",
            "ਮੈਨੂੰ ਭਲਕੇ ੧੫:੦੦ ਵਜੇ ਯਾਦ ਕਰਾਓ",
            "ನನಗೆ ನಾಳೆ ೧೫:೦೦ ಗಂಟೆಗೆ ನೆನಪಿಸಿ",
        ):
            with self.subTest(text=text):
                self.assertEqual(reminder_request_kind(text), "later follow-up")
                action = self.apply(text, rules=[CALLBACK_RULE, FOLLOWUP_RULE])[0]
                self.assertEqual(action["title"], "Customer Follow-up")
                self.assertEqual(action["due_at"], "2026-10-06T15:00:00+05:30")

    def test_explicit_relative_intervals_are_elapsed_time(self):
        for text in (
            "Ruf mich in 2 Stunden an", "मला २ तासांनंतर फोन करा",
            "ਮੈਨੂੰ ੨ ਘੰਟੇ ਬਾਅਦ ਕਾਲ ਕਰੋ", "ನನಗೆ ೨ ಗಂಟೆಗಳ ನಂತರ ಕರೆ ಮಾಡಿ",
        ):
            with self.subTest(text=text):
                action = self.apply(text)[0]
                self.assertEqual(datetime.fromisoformat(action["due_at"]), NOW + timedelta(hours=2))
        for text in ("in 10 Minuten", "१० मिनिटांनंतर", "੧੦ ਮਿੰਟ ਬਾਅਦ", "೧೦ ನಿಮಿಷಗಳ ನಂತರ"):
            with self.subTest(text=text):
                self.assertEqual(datetime.fromisoformat(self.parse(text)), NOW + timedelta(minutes=10))

    def test_explicit_dates_timezone_and_dst_guards_are_shared(self):
        self.assertEqual(self.apply("Ruf mich morgen um 15 Uhr an", timezone_name="Europe/Berlin")[0]["due_at"],
                         "2026-10-05T15:00:00+02:00")
        self.assertEqual(self.apply("ਮੈਨੂੰ 2026-10-06 15:00 UTC+02:00 ਕਾਲ ਕਰੋ")[0]["due_at"],
                         "2026-10-06T15:00:00+02:00")
        self.assertEqual(self.apply("ನನಗೆ ೨೦೨೬-೧೦-೦೬ ೧೫:೦೦ ಕರೆ ಮಾಡಿ")[0]["due_at"],
                         "2026-10-06T15:00:00+05:30")
        self.assertEqual(self.apply("Ruf mich 2026-10-25 um 02:30 Uhr an", timezone_name="Europe/Berlin"), [])

    def test_negative_conditional_and_factual_messages_never_authorize_actions(self):
        for text in (
            "Bitte rufen Sie mich morgen um 15 Uhr nicht an",
            "Wenn möglich, rufen Sie mich morgen um 15 Uhr nicht an",
            "Wenn du Zeit hast, ruf mich morgen um 15 Uhr an",
            "जर शक्य असेल, मला उद्या १५:०० वाजता फोन करा",
            "मला उद्या १५:०० वाजता फोन करू नका",
            "ਮੈਨੂੰ ਭਲਕੇ ੧੫:੦੦ ਵਜੇ ਕਾਲ ਨਾ ਕਰੋ",
            "ನನಗೆ ನಾಳೆ ೧೫:೦೦ ಗಂಟೆಗೆ ಕರೆ ಮಾಡಬೇಡಿ",
            "Meine Besprechung ist morgen um 15 Uhr",
            "माझी बैठक उद्या १५:०० वाजता आहे",
            "ਮੇਰੀ ਮੀਟਿੰਗ ਭਲਕੇ ੧੫:੦੦ ਵਜੇ ਹੈ",
            "ನನ್ನ ಸಭೆ ನಾಳೆ ೧೫:೦೦ ಗಂಟೆಗೆ ಇದೆ",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.apply(text, rules=[CALLBACK_RULE]), [])

    def test_alternatives_and_ambiguous_unsupported_or_past_dates_are_not_invented(self):
        for text in (
            "Ruf mich morgen oder übermorgen um 15 Uhr an",
            "मला उद्या किंवा आज १५:०० वाजता फोन करा",
            "ਮੈਨੂੰ ਭਲਕੇ ਜਾਂ ਅੱਜ ੧੫:੦੦ ਵਜੇ ਕਾਲ ਕਰੋ",
            "ನನಗೆ ನಾಳೆ ಅಥವಾ ಇಂದು ೧೫:೦೦ ಗಂಟೆಗೆ ಕರೆ ಮಾಡಿ",
            "Ruf mich morgen um 3 Uhr an", "मला उद्या ३ वाजता फोन करा",
            "ਮੈਨੂੰ ਕੱਲ੍ਹ ੧੫:੦੦ ਵਜੇ ਕਾਲ ਕਰੋ", "ನನಗೆ ನಿನ್ನೆ ೧೫:೦೦ ಗಂಟೆಗೆ ಕರೆ ಮಾಡಿ",
            "मला उद्या रात्री १ वाजता फोन करा", "ਨನಗೆ ನಾಳೆ ರಾತ್ರಿ ೧ ಗಂಟೆಗೆ ಕರೆ ಮಾಡಿ",
            "Ruf mich gestern um 15 Uhr an", "मला काल १५:०० वाजता फोन करा",
            "Ruf mich um 15 Uhr an", "Ruf mich 2030-02-31 um 15 Uhr an",
            "Ruf mich nächsten Monat um 15 Uhr an",
            "Ruf mich in 2 Stunden um 15:00 Uhr an",
            "Ruf mich morgen in 2 Stunden an",
            "Ruf mich am Morgen um 9 AM an",
            "Ruf mich in 2 Stunden in 3 Tagen an",
            "Ruf mich morgen um 15:00 Uhr um 5 PM an",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.apply(text, rules=[CALLBACK_RULE]), [])

    def test_multiple_requests_and_unrelated_dates_do_not_supply_a_time(self):
        for text in (
            "Ruf mich an, meine Besprechung ist morgen um 15 Uhr",
            "Ruf mich morgen um 15 Uhr an; ruf mich übermorgen um 16 Uhr an",
            "ਮੈਨੂੰ ਕਾਲ ਕਰੋ; ਮੇਰੀ ਮੀਟਿੰਗ ਭਲਕੇ ੧੫:੦੦ ਵਜੇ ਹੈ",
            "मला फोन करा; माझी बैठक उद्या १५:०० वाजता आहे",
            "ನನಗೆ ಕರೆ ಮಾಡಿ; ನನ್ನ ಸಭೆ ನಾಳೆ ೧೫:೦೦ ಗಂಟೆಗೆ ಇದೆ",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.apply(text), [])
