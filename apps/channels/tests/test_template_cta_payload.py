from urllib.parse import urlsplit

from django.test import TestCase, override_settings

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.organizations.models import Organization
from services.channels.template_cta_tracking import TRACKING_PATH, decode_cta_token
from services.channels.template_service import build_meta_payload


@override_settings(OPERATIONS_PUBLIC_ORIGIN="https://dashboard.shvya-ai.com")
class TemplateCTAPayloadTests(TestCase):
    def setUp(self):
        organization = Organization.objects.create(
            package="dfy",
            name="CTA Payload Test",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=organization,
            business_name="Test Sender",
        )
        self.organization = organization

    def make_template(self, *, name, category, buttons):
        template = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            name=name,
            category=category,
            body="Hello",
            buttons=buttons,
        )
        WhatsAppTemplateMetadata.objects.create(template=template)
        return template

    @staticmethod
    def buttons(payload):
        component = next(
            row for row in payload["components"] if row.get("type") == "BUTTONS"
        )
        return component["buttons"]

    def test_marketing_cta_uses_signed_url_and_keeps_quick_reply_native(self):
        template = self.make_template(
            name="tracked_marketing_cta",
            category=WhatsAppTemplate.Category.MARKETING,
            buttons=[
                {
                    "type": "visit_website",
                    "text": "View offer",
                    "url": "https://example.test/offer",
                },
                {"type": "text_back", "text": "Questions"},
            ],
        )

        buttons = self.buttons(build_meta_payload(template=template))
        self.assertEqual([row["type"] for row in buttons], ["URL", "QUICK_REPLY"])
        path = urlsplit(buttons[0]["url"]).path
        token = path.split(TRACKING_PATH, 1)[1].split("/", 1)[0]
        self.assertEqual(decode_cta_token(token)["a"], "url")

    def test_authentication_copy_code_remains_native(self):
        template = self.make_template(
            name="native_authentication_code",
            category=WhatsAppTemplate.Category.AUTHENTICATION,
            buttons=[{"type": "copy_offer", "coupon_code": "TESTCODE"}],
        )

        self.assertEqual(
            self.buttons(build_meta_payload(template=template)),
            [{"type": "COPY_CODE", "example": "TESTCODE"}],
        )
