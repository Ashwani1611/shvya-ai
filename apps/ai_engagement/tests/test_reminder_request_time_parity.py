"""Customer-request, authored-policy and timezone boundaries shared by channels."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.services.crm_routing_reliability import _ensure_datetime_reminder
from apps.ai_engagement.services.engagement_instruction_policy import section_lines
from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at


NOW = datetime(2026, 10, 4, 19, 30, tzinfo=datetime_timezone.utc)
CALLBACK_RULE = (
    "Reminder 1:\n"
    "- Create when the customer explicitly requests a callback and provides or confirms a future date and time.\n"
    "- Title: Customer Callback."
)


class ReminderRequestTimeParityTests(SimpleTestCase):
    def context(self, text, *, channel="whatsapp", timezone_name="Asia/Kolkata", previous=None):
        messages = [previous] if previous else []
        messages.append({"id": "current", "direction": "inbound", "body": text})
        return SimpleNamespace(
            organization={"id": "org", "timezone": timezone_name},
            stage={"id": "new", "name": "New leads"},
            pipeline={"attribute_definitions": [], "available_stages": []},
            conversation={"channel": channel, "messages": messages},
        )

    def apply(self, text, *, rules=None, actions=None, **context_kwargs):
        controlled = list(actions or [])
        context = self.context(text, **context_kwargs)
        with patch("apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=NOW):
            _ensure_datetime_reminder(controlled, text,
                {"crm": {"reminders": rules or []}}, context=context)
        return controlled

    def test_factual_dates_and_undated_requests_do_not_create_reminders(self):
        for text in (
            "My event is on 6 October 2026 at 3 PM",
            "My callback is tomorrow at 3 PM",
            "Please call me", "Call me at 3 PM", "kal 3 PM", "Tomorrow 5 PM works for me",
            "Kal 3 PM call mat karna", "Do not call me tomorrow at 5 PM",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.apply(text), [])

    def test_model_timestamp_cannot_create_reminder_without_current_request(self):
        proposed = {"type": "create_reminder", "title": "Callback", "description": "",
                    "due_at": "2099-10-06T15:00:00+05:30"}
        self.assertEqual(self.apply("My event is tomorrow at 5 PM", actions=[proposed]), [])
        self.assertEqual(self.apply("Please call me", actions=[proposed]), [])
        action = self.apply("Please call me tomorrow at 5 PM", actions=[proposed])[0]
        self.assertEqual(action["due_at"], "2026-10-06T17:00:00+05:30")

    def test_authored_title_and_hinglish_time_are_identical_across_runtime_channels(self):
        # These all enter the same installed policy builder. Transport sends are
        # independently tested at their provider boundaries.
        for channel in ("whatsapp_api", "coexistence", "hosted", "instagram", "sandbox"):
            context = self.context("Mujhe kal shaam 5 baje call karna", channel=channel)
            with self.subTest(channel=channel), patch(
                "apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=NOW,
            ):
                actions, _ = build_controlled_actions(
                    decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
                    context=context, runtime_policy={"crm": {"reminders": [CALLBACK_RULE]}},
                    qualification_state={"engagement_mode": "conversation", "requirement_states": {}},
                    requirements=[],
                )
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]["title"], "Customer Callback")
                self.assertEqual(actions[0]["due_at"], "2026-10-06T17:00:00+05:30")

    def test_date_confirmation_is_bound_to_immediately_prior_sent_callback_prompt(self):
        prompt = {"id": "prompt", "direction": "outbound", "status": "sent",
                  "body": "What date and time should I call you?"}
        action = self.apply("Tomorrow 5 PM works for me", rules=[CALLBACK_RULE], previous=prompt)[0]
        self.assertEqual(action["title"], "Customer Callback")
        self.assertEqual(action["due_at"], "2026-10-06T17:00:00+05:30")
        self.assertEqual(self.apply("Tomorrow 5 PM works for me", previous={**prompt, "status": "failed"}), [])
        dated_prompt = {**prompt, "body": "Shall I call you on 6 October 2026 at 5 PM?"}
        self.assertEqual(self.apply("Yes", previous=dated_prompt)[0]["due_at"], "2026-10-06T17:00:00+05:30")
        self.assertEqual(self.apply("No", previous=dated_prompt), [])
        brochure_prompt = {**prompt,
            "body": "The group call is on 6 October 2026 at 5 PM. Would you like the brochure?"}
        self.assertEqual(self.apply("Yes", previous=brochure_prompt), [])
        for rejected in ("Tomorrow I am busy at 5 PM", "Tomorrow at 5 PM does not work for me",
                         "6 October 2026 at 5 PM is unavailable", "Kal 5 PM available nahi hun"):
            with self.subTest(rejected=rejected):
                self.assertEqual(self.apply(rejected, previous=prompt), [])

    def test_compiled_reminder_section_preserves_condition_and_title_for_backend_request(self):
        rules = section_lines("## Reminder creation logic\n" + CALLBACK_RULE, "reminders")
        self.assertEqual(rules, [CALLBACK_RULE])
        action = self.apply("Please call me on 6 October 2026 at 3 PM", rules=rules)[0]
        self.assertEqual(action["title"], "Customer Callback")

    def test_org_timezone_and_explicit_customer_timezone_control_date_and_time(self):
        action = self.apply("Call me tomorrow at 5 PM", timezone_name="America/New_York")[0]
        self.assertEqual(action["due_at"], "2026-10-05T17:00:00-04:00")
        action = self.apply("Call me on 6 October 2026 at 5 PM UTC+02:00")[0]
        self.assertEqual(action["due_at"], "2026-10-06T17:00:00+02:00")
        action = self.apply("Call me on 6 October 2026 at 5 PM America/New_York")[0]
        self.assertEqual(action["due_at"], "2026-10-06T17:00:00-04:00")

    def test_live_hinglish_callback_and_brochure_request_keeps_requested_time(self):
        text = "Kal dopahar 3 baje India time par callback chahiye aur product brochure bhi bhej do."
        action = self.apply(text, rules=[CALLBACK_RULE], timezone_name="UTC")[0]
        self.assertEqual(action["due_at"], "2026-10-06T15:00:00+05:30")
        self.assertEqual(action["title"], "Customer Callback")
        self.assertEqual(self.apply("kal ka invoice 3 baje milega"), [])

    def test_business_negations_do_not_cancel_explicit_callback_request(self):
        for text in ("I am not running ads. Call me tomorrow at 3 PM",
                     "Ads nahi chal rahe, kal dopahar 3 baje callback chahiye",
                     "Ads nahi chal rahe aur kal dopahar 3 baje callback chahiye"):
            with self.subTest(text=text):
                self.assertEqual(self.apply(text, rules=[CALLBACK_RULE])[0]["due_at"],
                                 "2026-10-06T15:00:00+05:30")

    def test_positive_request_uses_its_own_time_excluding_negated_or_unrelated_dates(self):
        for text in (
            "Do not call me tomorrow at 5 PM; call me on 9 October 2026 at 3 PM",
            "Please call me on 9 October 2026 at 3 PM; I am not free tomorrow at 5 PM",
            "I missed your call yesterday; call me on 9 October 2026 at 3 PM",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.apply(text, rules=[CALLBACK_RULE])[0]["due_at"],
                                 "2026-10-09T15:00:00+05:30")
        self.assertEqual(self.apply("Call me tomorrow at 5 PM or on 9 October 2026 at 3 PM, whichever works"), [])
        self.assertEqual(self.apply("Call me tomorrow at 5 PM, or on 9 October 2026 at 3 PM"), [])

    def test_only_explicit_org_immediate_rule_allows_immediate_callback(self):
        rule = CALLBACK_RULE.replace("and provides or confirms a future date and time", "immediately")
        self.assertEqual(self.apply("Call me now", rules=[CALLBACK_RULE]), [])
        self.assertEqual(self.apply("Call me", rules=[rule]), [])
        action = self.apply("Call me now", rules=[rule])[0]
        self.assertEqual(datetime.fromisoformat(action["due_at"]), NOW)
        self.assertEqual(action["title"], "Customer Callback")


class GroundedReminderTimezoneTests(SimpleTestCase):
    def parse(self, text, timezone_name="Asia/Kolkata"):
        with patch("apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=NOW):
            return parse_grounded_due_at(text, timezone_name=timezone_name)

    def test_unknown_missing_invalid_and_past_dates_are_not_guessed(self):
        for text in (
            "Call me at 5 PM", "Call me next month at 5 PM", "Call me 31 February 2030 at 5 PM",
            "Call me 2026-10-03 at 5 PM", "Call me yesterday at 5 PM", "Kal 5 PM call hua tha",
            "Call me 6 October 2026 at 5 PM EST", "Call me 6 October 2026 at 5 PM UTC+05:75",
            "Call me 6 October 2026 at 5 PM timezone Mars/Olympus", "Call me 6 October 2026 UTC+05:30",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))
        self.assertIsNone(self.parse("Call me tomorrow 5 PM", timezone_name="Not/AZone"))

    def test_explicit_iso_offset_and_hinglish_relative_times(self):
        self.assertEqual(self.parse("Call me 2026-10-06T17:00:00+02:00"), "2026-10-06T17:00:00+02:00")
        for text, delta in (("2 ghante baad call karna", timedelta(hours=2)),
                            ("Call me after two hours", timedelta(hours=2)),
                            ("do ghante baad call karna", timedelta(hours=2)),
                            ("2 din baad remind me", timedelta(days=2)),
                            ("1 hafte baad call karna", timedelta(weeks=1))):
            with self.subTest(text=text):
                self.assertEqual(datetime.fromisoformat(self.parse(text)), NOW + delta)

    def test_ambiguous_dst_wall_clock_time_is_not_scheduled(self):
        self.assertIsNone(self.parse("Call me 1 November 2026 at 1:30 AM", "America/New_York"))
        self.assertIsNone(self.parse("Call me 14 March 2027 at 2:30 AM", "America/New_York"))

    def test_urls_and_commercial_units_are_not_timezone_names(self):
        self.assertEqual(self.parse("Call me tomorrow at 5 PM about Rs/month https://example.com/Asia/Kolkata"),
                         "2026-10-06T17:00:00+05:30")

    def test_valid_iana_zone_names_keep_hyphens_and_zoneinfo_offset_convention(self):
        self.assertEqual(self.parse("Call me tomorrow 5 PM America/Port-au-Prince"),
                         "2026-10-05T17:00:00-04:00")
        self.assertEqual(self.parse("Call me tomorrow 5 PM Etc/GMT+5"),
                         "2026-10-05T17:00:00-05:00")

    def test_relative_hours_mean_elapsed_time_across_dst_transition(self):
        before_dst = datetime(2027, 3, 14, 6, 30, tzinfo=datetime_timezone.utc)
        with patch("apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=before_dst):
            due = datetime.fromisoformat(parse_grounded_due_at("Call me in 2 hours", timezone_name="America/New_York"))
        self.assertEqual(due.astimezone(datetime_timezone.utc), before_dst + timedelta(hours=2))
