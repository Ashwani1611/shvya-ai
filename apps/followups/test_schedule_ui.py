from bs4 import BeautifulSoup
from django.template import Context, Template
from django.test import SimpleTestCase


class FollowupScheduleTemplateTests(SimpleTestCase):
    def test_schedule_partial_has_mutually_exclusive_schedule_controls(self):
        choices = type(
            "Choices",
            (),
            {
                "choices": [
                    (0, "Sunday"),
                    (1, "Monday"),
                    (2, "Tuesday"),
                ]
            },
        )
        template = Template('{% include "followups/partials/schedule_fields.html" %}')
        rendered = template.render(
            Context(
                {
                    "delay_units": choices,
                    "weekdays": choices,
                }
            )
        )

        document = BeautifulSoup(rendered, "html.parser")
        schedule_controls = document.select('input[name="schedule_type"]')
        self.assertEqual(len(schedule_controls), 4)
        self.assertTrue(all(control.get("type") == "radio" for control in schedule_controls))
        self.assertCountEqual(
            [control.get("value") for control in schedule_controls],
            ["immediate", "specific_time", "delay", "recurring"],
        )
        self.assertEqual(
            [control.get("value") for control in schedule_controls if control.has_attr("checked")],
            ["immediate"],
        )
        self.assertIn('value="immediate"', rendered)
        self.assertIn('value="specific_time"', rendered)
        self.assertIn('value="delay"', rendered)
        self.assertIn('value="recurring"', rendered)
        self.assertIn('data-schedule-panel="immediate"', rendered)
        self.assertIn('data-schedule-panel="specific_time"', rendered)
        self.assertIn('data-schedule-panel="delay"', rendered)
        self.assertIn('data-schedule-panel="recurring"', rendered)
        self.assertIn('name="recurring_mode"', rendered)
        self.assertIn('value="specific_days"', rendered)
        self.assertIn('value="interval"', rendered)
        self.assertIn("Next available", rendered)
        self.assertIn("Monday", rendered)
        self.assertIn("Tuesday", rendered)
