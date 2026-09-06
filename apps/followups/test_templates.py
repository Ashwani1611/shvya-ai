from django.template.loader import get_template
from django.test import SimpleTestCase
from django.urls import reverse


class AutoFollowupTemplateTests(SimpleTestCase):
    def test_sequence_templates_compile(self):
        for template_name in [
            "followups/sequence_list.html",
            "followups/sequence_create.html",
            "followups/sequence_edit.html",
            "followups/partials/template_picker.html",
            "followups/partials/template_preview.html",
            "followups/partials/email_step_modal.html",
            "followups/partials/reminder_step_modal.html",
            "followups/partials/settings_modal.html",
            "followups/partials/lead_control.html",
            "followups/partials/schedule_fields.html",
            "followups/partials/personalization_chips.html",
        ]:
            with self.subTest(template=template_name):
                self.assertIsNotNone(get_template(template_name))

    def test_sequence_list_uses_expected_dashboard_url(self):
        self.assertEqual(
            reverse("crm-auto-follow-ups-sequences"),
            "/dashboard/auto-follow-ups/sequences/",
        )
