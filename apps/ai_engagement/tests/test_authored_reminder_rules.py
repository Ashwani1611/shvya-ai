from unittest.mock import patch
from django.test import SimpleTestCase
from apps.ai_engagement.services.authored_reminder_rules import requested_reminder


class AuthoredReminderRulesTests(SimpleTestCase):
    rules = [
        "Reminder 1:\n- Create when the customer explicitly requests a callback and provides or confirms a future date and time.\n- Title: Customer Callback.",
        "Reminder 2:\n- Create when the customer explicitly requests a later follow-up and provides or confirms a future date and time.\n- Title: Organization Follow-up.",
    ]

    def resolve(self, text, rules=None):
        with patch("apps.ai_engagement.services.reminder_time_runtime.parse_grounded_due_at", return_value="2099-10-06T15:00:00+05:30"):
            return requested_reminder(rules=self.rules if rules is None else rules, text=text)

    def test_title_comes_from_authored_matching_rule(self):
        self.assertEqual(self.resolve("Please follow up with me on 6 October at 3 PM")["title"], "Organization Follow-up")
        self.assertEqual(self.resolve("Please call me on 6 October at 3 PM")["title"], "Customer Callback")

    def test_dates_and_negated_requests_do_not_authorize_reminders(self):
        self.assertIsNone(self.resolve("My event is on 6 October at 3 PM"))
        self.assertIsNone(self.resolve("Do not call me on 6 October at 3 PM"))

    def test_extra_authored_conditions_are_not_silently_discarded(self):
        rules = [self.rules[0].replace("requests a callback", "requests a callback as a VIP customer")]
        self.assertIsNone(self.resolve("Call me on 6 October at 3 PM", rules))

    def test_missing_or_past_time_does_not_create_an_immediate_callback(self):
        for due in (None, "2000-10-06T15:00:00+05:30"):
            with self.subTest(due=due), patch("apps.ai_engagement.services.reminder_time_runtime.parse_grounded_due_at", return_value=due):
                self.assertIsNone(requested_reminder(rules=self.rules, text="Please call me"))
