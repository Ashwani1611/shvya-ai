from django.test import TestCase, override_settings

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.organizations.models import Organization
from services.channels import template_service


@override_settings(CTA_TRACKING_PUBLIC_BASE_URL="https://track.shvya.example")
class WhatsAppTemplateDynamicURLContractTests(TestCase):
    def test_tracking_template_uses_suffix_example_not_expanded_url(self):
        organization = Organization.objects.create(name="Dynamic URL Contract")
        account = WhatsAppAccount.objects.create(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Dynamic URL Business",
            phone_number_id="phone-dynamic-url",
            waba_id="waba-dynamic-url",
            access_token="dynamic-url-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        template = WhatsAppTemplate.objects.create(
            organization=organization,
            account=account,
            name="dynamic_url_contract",
            category=WhatsAppTemplate.Category.MARKETING,
            body="Open your offer",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open offer",
                    "url": "https://example.com/offer",
                }
            ],
        )

        payload = template_service.build_meta_payload(template=template)
        buttons = next(
            component["buttons"]
            for component in payload["components"]
            if component["type"] == "BUTTONS"
        )
        button = buttons[0]

        self.assertEqual(
            button["url"],
            "https://track.shvya.example/w/cta/{{1}}",
        )
        self.assertEqual(
            button["example"],
            ["00000000-0000-4000-8000-000000000001"],
        )
        self.assertNotIn("https://", button["example"][0])
