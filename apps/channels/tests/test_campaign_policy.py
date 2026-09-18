"""Pure policy regression tests; safe to execute without Django or a provider."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from services.channels.campaign_policy import (
    CampaignInputError, csv_cell, fingerprint, integer, media_parameter,
    percent, render_message, retry_decision, scheduled_time, template_fields,
)


class CampaignPolicyTests(TestCase):
    def setUp(self):
        self.spec = {"components": [{"type": "BODY", "text": "Hello {{1}}"}], "placeholder_mapping": {"1": "lead_name"}}
        self.binding = {"body.1": {"source": "lead_name", "default": "Friend"}}
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_preview_and_actual_body_components_use_same_values(self):
        result = render_message(self.spec, self.binding, {"lead_name": "Asha & <Team>"}, {"lead_name"})
        self.assertEqual(result["body"], "Hello Asha & <Team>")
        self.assertEqual(result["components"], [{"type": "body", "parameters": [{"type": "text", "text": "Asha & <Team>"}]}])

    def test_fallback_only_when_value_missing_and_zero_is_retained(self):
        for value, expected in [(None, "Friend"), ("", "Friend"), ("  ", "Friend"), (0, "0"), (False, "False")]:
            with self.subTest(value=value):
                self.assertEqual(render_message(self.spec, self.binding, {"lead_name": value}, {"lead_name"})["body"], f"Hello {expected}")

    def test_missing_or_internal_bindings_are_rejected(self):
        for bindings in [None, [], {"body.2": {}}, {"body.1": {"source": "_runtime"}}, {"body.1": {"default": {}}}, {"body.1": {"default": "x" * 2049}}]:
            with self.subTest(bindings=bindings), self.assertRaises(CampaignInputError):
                render_message(self.spec, bindings, {}, {"lead_name"})

    def test_numeric_tokens_are_in_parameter_order_not_text_order(self):
        self.spec["components"][0]["text"] = "{{2}} then {{1}} and {{2}}"
        result = render_message(self.spec, {"body.1": {"default": "First"}, "body.2": {"default": "Second"}}, {}, set())
        self.assertEqual(result["body"], "Second then First and Second")
        self.assertEqual([p["text"] for p in result["components"][0]["parameters"]], ["First", "Second"])

    def test_named_header_body_url_and_coupon_parameters(self):
        spec = {"components": [{"type": "HEADER", "format": "TEXT", "text": "For {{person}}"},
                               {"type": "BODY", "text": "Hi {{name}}"}, {"type": "FOOTER", "text": "SHVYA"},
                               {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "Open", "url": "https://example.com/{{1}}"},
                                                                 {"type": "COPY_CODE"}, {"type": "QUICK_REPLY", "text": "Interested"}]}]}
        fields = template_fields(spec)
        bindings = {field["key"]: {"default": "Value"} for field in fields}
        result = render_message(spec, bindings, {}, set())
        self.assertEqual(result["components"][0]["parameters"][0]["parameter_name"], "person")
        self.assertEqual(result["components"][2]["sub_type"], "url")
        self.assertEqual(result["components"][3]["parameters"][0]["type"], "coupon_code")
        self.assertIn("[Interested]", result["body"])

    def test_message_time_media_and_carousel_components(self):
        spec = {"components": [{"type": "BODY", "text": "Options"}, {"type": "CAROUSEL", "cards": [
            {"components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "{{1}}"}]}]}]}
        result = render_message(spec, {"card0.header.media": {"default": "https://example.com/image.jpg"}, "card0.body.1": {"default": "Option"}}, {}, set())
        card = result["components"][0]["cards"][0]
        self.assertEqual(card["card_index"], 0)
        self.assertEqual(card["components"][0]["parameters"][0]["image"]["link"], "https://example.com/image.jpg")
        self.assertIn("Card 1", result["body"])

    def test_private_media_and_unsupported_template_parts_are_rejected(self):
        for url in ["http://example.com/a", "https://127.0.0.1/a", "https://user:pass@example.com/a", "https://localhost/a", "javascript:alert(1)"]:
            with self.subTest(url=url), self.assertRaises(CampaignInputError):
                media_parameter(url, "image")
        self.assertEqual(media_parameter("id:123456", "document"), {"type": "document", "document": {"id": "123456"}})
        for component in [{"type": "UNKNOWN"}, {"type": "HEADER", "format": "LOCATION"},
                          {"type": "FOOTER", "text": "{{1}}"}, {"type": "BUTTONS", "buttons": [{"type": "OTP"}]}]:
            with self.subTest(component=component), self.assertRaises(CampaignInputError):
                template_fields({"components": [component]})

    def test_static_template_needs_no_invented_parameters(self):
        result = render_message({"components": [{"type": "BODY", "text": "A confirmed update."}]}, {}, {}, set())
        self.assertEqual(result, {"body": "A confirmed update.", "components": []})

    def test_schedule_converts_to_utc_and_rejects_bad_dates(self):
        self.assertEqual(scheduled_time("2026-01-02T10:00", "Asia/Kolkata", now=self.now), datetime(2026, 1, 2, 4, 30, tzinfo=timezone.utc))
        for value, zone in [("2025-01-01T10:00", "UTC"), ("2028-01-01T10:00", "UTC"), ("2026-01-02T10:00+05:30", "Asia/Kolkata"), ("2026-01-02T10:00", "Invalid/Zone"), ("invalid", "UTC")]:
            with self.subTest(value=value), self.assertRaises(CampaignInputError):
                scheduled_time(value, zone, now=self.now)

    def test_dst_gap_and_duplicate_hour_are_not_silently_shifted(self):
        for value in ["2026-03-08T02:30", "2026-11-01T01:30"]:
            with self.subTest(value=value), self.assertRaises(CampaignInputError):
                scheduled_time(value, "America/New_York", now=self.now)

    def test_retry_limits_and_error_specific_cooldown(self):
        base = dict(code="131049", http_status=400, uncertain=False, attempts=1, maximum_retries=3, failed_at=self.now, delay_hours=1, now=self.now)
        allowed, due, _ = retry_decision(**base)
        self.assertTrue(allowed)
        self.assertEqual(due, self.now + timedelta(hours=24))
        for changes in [{"uncertain": True}, {"attempts": 4}, {"code": "131026"}, {"maximum_retries": 0}]:
            with self.subTest(changes=changes):
                self.assertFalse(retry_decision(**{**base, **changes})[0])
        self.assertTrue(retry_decision(**{**base, "code": "", "http_status": 503})[0])

    def test_digests_percentages_and_spreadsheet_formula_safety(self):
        self.assertEqual(fingerprint({"b": 2, "a": 1}), fingerprint({"a": 1, "b": 2}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))
        self.assertEqual(percent(1, 3), 33.33)
        self.assertEqual(percent(0, 0), 0)
        for value in ["=1+1", "+919000000001", "@SUM(1)", " -2", "\t=1+1"]:
            with self.subTest(value=value):
                self.assertTrue(csv_cell(value).startswith("'"))
        self.assertEqual(csv_cell(None), "")
        self.assertEqual(csv_cell("Normal text"), "Normal text")

    def test_integer_bounds_reject_booleans_fractional_and_negative_values(self):
        self.assertEqual(integer("3", minimum=0, maximum=3, label="Retries"), 3)
        for value in [True, 1.2, -1, "4", "not-a-number"]:
            with self.subTest(value=value), self.assertRaises(CampaignInputError):
                integer(value, minimum=0, maximum=3, label="Retries")
