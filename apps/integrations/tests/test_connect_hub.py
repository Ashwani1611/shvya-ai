from django.test import SimpleTestCase
from django.urls import reverse

from apps.integrations.views.web import CONNECT_HUB_GROUPS


class ConnectHubURLTests(SimpleTestCase):
    def test_connect_hub_and_integration_routes(self):
        expected_routes = {
            "crm-connect-hub": "/dashboard/connect-hub/",
            "crm-connect-hub-shvya-api": "/dashboard/connect-hub/shvya-api/",
            "crm-connect-hub-webhook": "/dashboard/connect-hub/webhook/",
            "crm-connect-hub-google-sheets": "/dashboard/connect-hub/google-sheets/",
            "crm-connect-hub-email": "/dashboard/connect-hub/email/",
            "crm-connect-hub-meta-conversions-api": "/dashboard/connect-hub/meta-conversions-api/",
            "crm-connect-hub-meta-lead-ad-forms": "/dashboard/connect-hub/meta-lead-ad-forms/",
            "crm-connect-hub-razorpay": "/dashboard/connect-hub/razorpay/",
            "crm-connect-hub-justdial": "/dashboard/connect-hub/justdial/",
            "crm-connect-hub-indiamart": "/dashboard/connect-hub/indiamart/",
            "crm-integrations-hub": "/dashboard/integrations-hub/",
        }

        for route_name, expected_path in expected_routes.items():
            with self.subTest(route_name=route_name):
                self.assertEqual(reverse(route_name), expected_path)

    def test_connect_hub_contains_requested_integrations(self):
        names = {
            item["name"]
            for group in CONNECT_HUB_GROUPS
            for item in group["items"]
        }

        self.assertEqual(
            names,
            {
                "Shvya API",
                "Webhook",
                "Google Sheets",
                "Email",
                "Meta Conversions API",
                "Meta Lead Ad Forms",
                "RazorPay",
                "Justdial",
                "IndiaMART",
            },
        )
