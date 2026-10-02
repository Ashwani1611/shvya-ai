import re
from datetime import date
from urllib.parse import urlsplit

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_cta_models import WhatsAppTemplateCTAEvent
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.organizations.models import Organization
from services.channels.template_analytics import fetch_template_analytics
from services.channels.template_cta_tracking import (
    TRACKING_PATH,
    decode_cta_token,
)
from services.channels.template_service import TemplateError, build_meta_payload


@override_settings(OPERATIONS_PUBLIC_ORIGIN="https://dashboard.shvya-ai.com")
class WhatsAppTemplateCTATrackingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            package="dfy",
            name="CTA Tracking Org",
        )
        self.user = User.objects.create_user(
            email="cta-tracking@example.com",
            password="test-password",
            name="CTA Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="CTA Sender",
            phone_number_id="123456789",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def template(self, *, name, buttons, category=WhatsAppTemplate.Category.MARKETING):
        template = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            created_by=self.user,
            name=name,
            category=category,
            status=WhatsAppTemplate.Status.DRAFT,
            body="Hello from SHVYA",
            buttons=buttons,
        )
        WhatsAppTemplateMetadata.objects.create(template=template)
        return template

    @staticmethod
    def buttons(payload):
        component = next(
            item
            for item in payload["components"]
            if item.get("type") == "BUTTONS"
        )
        return component["buttons"]

    @staticmethod
    def token_from_url(url):
        path = urlsplit(url).path
        return path.split(TRACKING_PATH, 1)[1].split("/", 1)[0]

    @staticmethod
    def form_token(response):
        match = re.search(
            rb'name="event_token" value="([^"]+)"',
            response.content,
        )
        if match is None:
            raise AssertionError("Tracked CTA confirmation token was not rendered.")
        return match.group(1).decode("utf-8")

    def test_website_and_call_become_signed_urls_while_quick_reply_stays_native(self):
        template = self.template(
            name="tracked_standard_ctas",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "View plans",
                    "url": "https://example.com/plans",
                },
                {
                    "type": "call_phone",
                    "text": "Call sales",
                    "phone_number": "+918700274739",
                },
                {"type": "text_back", "text": "Need help"},
            ],
        )

        payload = build_meta_payload(template=template)
        buttons = self.buttons(payload)

        self.assertEqual([item["type"] for item in buttons], ["URL", "URL", "QUICK_REPLY"])
        self.assertIn(TRACKING_PATH, buttons[0]["url"])
        self.assertIn(TRACKING_PATH, buttons[1]["url"])
        self.assertEqual(
            decode_cta_token(self.token_from_url(buttons[0]["url"]))["a"],
            "url",
        )
        self.assertEqual(
            decode_cta_token(self.token_from_url(buttons[1]["url"]))["a"],
            "call",
        )
        template.meta_state.refresh_from_db()
        self.assertEqual(template.meta_state.components, payload["components"])

    def test_copy_code_becomes_a_confirmed_tracked_action(self):
        template = self.template(
            name="tracked_copy_code",
            buttons=[
                {"type": "copy_offer", "coupon_code": "SAVE20"},
                {"type": "text_back", "text": "Questions"},
            ],
        )

        payload = build_meta_payload(template=template)
        buttons = self.buttons(payload)
        tracking = decode_cta_token(self.token_from_url(buttons[0]["url"]))

        self.assertEqual(buttons[0]["type"], "URL")
        self.assertEqual(tracking["a"], "copy_code")
        self.assertEqual(tracking["d"], "SAVE20")
        self.assertEqual(buttons[1]["type"], "QUICK_REPLY")

    def test_authentication_copy_code_keeps_native_meta_semantics(self):
        template = self.template(
            name="authentication_code",
            category=WhatsAppTemplate.Category.AUTHENTICATION,
            buttons=[{"type": "copy_offer", "coupon_code": "123456"}],
        )

        payload = build_meta_payload(template=template)

        self.assertEqual(self.buttons(payload), [{"type": "COPY_CODE", "example": "123456"}])

    def test_dynamic_website_parameter_must_be_https_host_safe_and_at_the_end(self):
        invalid_host = self.template(
            name="dynamic_host_rejected",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open",
                    "url": "https://{{lead_name}}/orders",
                }
            ],
        )
        with self.assertRaisesRegex(TemplateError, "cannot change the URL host"):
            build_meta_payload(template=invalid_host)

        invalid_position = self.template(
            name="dynamic_position_rejected",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open",
                    "url": "https://example.com/{{lead_name}}/details",
                }
            ],
        )
        with self.assertRaisesRegex(TemplateError, "must be at the end"):
            build_meta_payload(template=invalid_position)

        valid = self.template(
            name="dynamic_suffix_accepted",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open",
                    "url": "https://example.com/orders/{{lead_name}}",
                }
            ],
        )
        payload = build_meta_payload(template=valid)
        tracked_url = self.buttons(payload)[0]["url"]
        self.assertTrue(tracked_url.endswith("{{lead_name}}/"))

    def test_preview_get_does_not_count_and_confirmed_post_is_idempotent(self):
        template = self.template(
            name="public_click_receipt",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open portal",
                    "url": "https://example.com/portal",
                }
            ],
        )
        payload = build_meta_payload(template=template)
        template.status = WhatsAppTemplate.Status.APPROVED
        template.meta_template_id = "meta-public-click"
        template.save(update_fields=["status", "meta_template_id", "updated_at"])
        tracked_url = self.buttons(payload)[0]["url"]
        path = urlsplit(tracked_url).path

        preview = self.client.get(path)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(WhatsAppTemplateCTAEvent.objects.count(), 0)
        event_token = self.form_token(preview)

        confirmed = self.client.post(
            path,
            {"event_token": event_token},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["destination"], "https://example.com/portal")
        self.assertEqual(WhatsAppTemplateCTAEvent.objects.count(), 1)

        duplicate = self.client.post(
            path,
            {"event_token": event_token},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(WhatsAppTemplateCTAEvent.objects.count(), 1)

    def test_dynamic_confirmation_token_cannot_be_reused_for_another_destination(self):
        template = self.template(
            name="dynamic_confirmation_binding",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open order",
                    "url": "https://example.com/orders/{{lead_name}}",
                }
            ],
        )
        payload = build_meta_payload(template=template)
        template.status = WhatsAppTemplate.Status.APPROVED
        template.meta_template_id = "meta-dynamic-click"
        template.save(update_fields=["status", "meta_template_id", "updated_at"])
        tracked_url = self.buttons(payload)[0]["url"]
        first_path = urlsplit(tracked_url.replace("{{lead_name}}", "first-order")).path
        second_path = urlsplit(tracked_url.replace("{{lead_name}}", "second-order")).path

        preview = self.client.get(first_path)
        self.assertEqual(preview.status_code, 200)
        event_token = self.form_token(preview)
        tampered = self.client.post(
            second_path,
            {"event_token": event_token},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(tampered.status_code, 404)
        self.assertEqual(WhatsAppTemplateCTAEvent.objects.count(), 0)

    def test_confirmed_cta_receipts_fill_click_analytics_without_meta(self):
        template = self.template(
            name="local_cta_analytics",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "View offer",
                    "url": "https://example.com/offer",
                }
            ],
        )
        build_meta_payload(template=template)
        template.status = WhatsAppTemplate.Status.APPROVED
        template.meta_template_id = "meta-local-analytics"
        template.save(update_fields=["status", "meta_template_id", "updated_at"])
        WhatsAppTemplateCTAEvent.objects.create(
            organization=self.organization,
            account=self.account,
            template=template,
            action_type=WhatsAppTemplateCTAEvent.ActionType.URL,
            button_index=0,
            button_label="View offer",
            destination="https://example.com/offer",
            visitor_hash="a" * 64,
            request_key="b" * 64,
        )

        results = fetch_template_analytics(
            account=self.account,
            template_ids=[template.meta_template_id],
            start_date=date.today(),
            end_date=date.today(),
        )
        result = results[template.meta_template_id]

        self.assertTrue(result["availability"]["clicked"])
        self.assertEqual(result["totals"]["clicked"], 1)
        self.assertEqual(result["unique_click_total"], 1)
        self.assertEqual(
            result["clicks"],
            [
                {
                    "type": "url_button",
                    "button_content": "View offer",
                    "count": 1,
                }
            ],
        )
