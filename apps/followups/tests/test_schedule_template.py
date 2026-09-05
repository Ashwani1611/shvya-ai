from django.template import Context, Template
from django.test import SimpleTestCase


class FollowupScheduleTemplateTests(SimpleTestCase):
    def test_schedule_partial_has_one_radio_group_and_conditional_panels(self):
        template = Template(
            '{% include "followups/partials/schedule_fields.html" %}'
        )
        rendered = template.render(Context({"delay_units": type("Units", (), {"choices": []}), "weekdays": type("Days", (), {"choices": []})}))

        self.assertEqual(rendered.count('name="schedule_type"'), 3)
        self.assertIn('data-schedule-panel="immediate"', rendered)
        self.assertIn('data-schedule-panel="specific_time"', rendered)
        self.assertIn('data-schedule-panel="delay"', rendered)
        self.assertIn("Next available", rendered)
