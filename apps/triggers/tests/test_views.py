from django.template.loader import get_template
from django.test import SimpleTestCase
from django.urls import reverse


class SmartTriggerTemplateAndRouteTests(SimpleTestCase):
    def test_dashboard_routes_are_registered(self):
        self.assertEqual(reverse("crm-smart-triggers"), "/dashboard/smart-triggers/")
        self.assertEqual(
            reverse("smart-trigger-create"),
            "/dashboard/smart-triggers/new/",
        )

    def test_smart_trigger_templates_compile(self):
        self.assertIsNotNone(get_template("triggers/trigger_list.html"))
        self.assertIsNotNone(get_template("triggers/trigger_form.html"))
