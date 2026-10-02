from urllib.parse import urlsplit
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.organizations.models import Organization
from services.channels.template_cta_tracking import (
    TRACKING_PATH,
    decode_cta_token,
    source_buttons_for_template,
)
from services.channels.template_meta_fix import submit_template
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
            phone_number_id="phone-cta-test",
            waba_id="waba-cta-test",
            access_token="cta-test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
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

    @staticmethod
    def tracking_token(button):
        path = urlsplit(button["url"]).path
        return path.split(TRACKING_PATH, 1)[1].split("/", 1)[0]

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
        self.assertNotIn("example", buttons[0])
        token = self.tracking_token(buttons[0])
        self.assertRegex(token, r"^t2_[A-Za-z0-9_-]+$")
        self.assertEqual(decode_cta_token(token)["a"], "url")

    def test_dynamic_cta_uses_positional_url_and_suffix_only_example(self):
        template = self.make_template(
            name="tracked_dynamic_cta",
            category=WhatsAppTemplate.Category.MARKETING,
            buttons=[
                {
                    "type": "visit_website",
                    "text": "View order",
                    "url": "https://example.test/orders/{{lead_name}}",
                }
            ],
        )

        button = self.buttons(build_meta_payload(template=template))[0]

        self.assertTrue(button["url"].endswith("{{1}}"))
        self.assertNotIn("{{lead_name}}", button["url"])
        self.assertEqual(button["example"], ["sample"])
        self.assertNotIn("https://", button["example"][0])
        token = self.tracking_token(button)
        self.assertRegex(token, r"^t2_[A-Za-z0-9_-]+$")
        token_data = decode_cta_token(token)
        self.assertEqual(token_data["p"], "lead_name")
        self.assertEqual(
            token_data["d"],
            "https://example.test/orders/{{lead_name}}",
        )

    def test_remote_dynamic_url_example_is_preserved_as_suffix(self):
        template = self.make_template(
            name="tracked_remote_dynamic_cta",
            category=WhatsAppTemplate.Category.MARKETING,
            buttons=[],
        )
        state = template.meta_state
        state.components = [
            {"type": "BODY", "text": "Hello"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL",
                        "text": "View offer",
                        "url": "https://example.test/offers/{{1}}",
                        "example": ["summer2023"],
                    }
                ],
            },
        ]
        state.save(update_fields=["components", "updated_at"])

        template.buttons = source_buttons_for_template(template)
        template.save(update_fields=["buttons", "updated_at"])
        button = self.buttons(build_meta_payload(template=template))[0]

        self.assertTrue(button["url"].endswith("{{1}}"))
        self.assertEqual(button["example"], ["summer2023"])

    def test_call_and_copy_code_replacements_are_static_tracked_urls(self):
        template = self.make_template(
            name="tracked_call_and_code",
            category=WhatsAppTemplate.Category.MARKETING,
            buttons=[
                {
                    "type": "call_phone",
                    "text": "Call us",
                    "phone_number": "+919876543210",
                },
                {
                    "type": "copy_offer",
                    "text": "Copy code",
                    "coupon_code": "SAVE20",
                },
            ],
        )

        buttons = self.buttons(build_meta_payload(template=template))

        self.assertEqual([button["type"] for button in buttons], ["URL", "URL"])
        self.assertTrue(all("example" not in button for button in buttons))
        self.assertEqual(
            [decode_cta_token(self.tracking_token(button))["a"] for button in buttons],
            ["call", "copy_code"],
        )

    @patch("services.channels.template_service.WhatsAppClient._post")
    def test_save_and_submit_sends_meta_valid_dynamic_cta_payload(self, post):
        post.return_value = {"id": "meta-tracked-cta", "status": "PENDING"}
        template = self.make_template(
            name="submit_tracked_dynamic_cta",
            category=WhatsAppTemplate.Category.MARKETING,
            buttons=[
                {
                    "type": "visit_website",
                    "text": "View order",
                    "url": "https://example.test/orders/{{lead_name}}",
                }
            ],
        )

        submit_template(template=template)

        payload = post.call_args.args[1]
        button = self.buttons(payload)[0]
        self.assertTrue(button["url"].endswith("{{1}}"))
        self.assertEqual(button["example"], ["sample"])
        self.assertNotRegex(button["example"][0], r"^https?://")
        template.refresh_from_db()
        self.assertEqual(template.meta_template_id, "meta-tracked-cta")
        self.assertEqual(template.status, WhatsAppTemplate.Status.PENDING)

    def test_public_tracking_url_accepts_head_without_recording_click(self):
        template = self.make_template(
            name="tracked_head_probe",
            category=WhatsAppTemplate.Category.MARKETING,
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open",
                    "url": "https://example.test/open",
                }
            ],
        )
        button = self.buttons(build_meta_payload(template=template))[0]
        path = urlsplit(button["url"]).path

        response = self.client.head(path)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["Content-Length"], "0")

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
