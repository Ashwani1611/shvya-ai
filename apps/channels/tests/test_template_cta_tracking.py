from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.tracking_models import (
    WhatsAppTemplateTrackedClick,
    WhatsAppTemplateTrackedLink,
)
from apps.organizations.models import Organization
from services.channels import campaign_policy, template_service
from services.channels.template_cta_tracking import (
    augment_tracked_cta_analytics,
    prepare_message_tracking,
    tracking_url_template,
)


@override_settings(
    CTA_TRACKING_PUBLIC_BASE_URL="https://track.shvya.example",
)
class WhatsAppTemplateCTATrackingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Tracked CTA Org")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Tracked CTA Business",
            phone_number_id="phone-tracked-cta",
            waba_id="waba-tracked-cta",
            access_token="tracked-cta-token",
            display_phone_number="+911111111111",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _template(
        self,
        *,
        name,
        buttons,
        category=WhatsAppTemplate.Category.MARKETING,
        status=WhatsAppTemplate.Status.DRAFT,
        meta_template_id="",
    ):
        return WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            name=name,
            category=category,
            status=status,
            body="Choose an action",
            buttons=buttons,
            meta_template_id=meta_template_id,
        )

    @staticmethod
    def _buttons(payload):
        component = next(
            item
            for item in payload["components"]
            if str(item.get("type") or "").upper() == "BUTTONS"
        )
        return component["buttons"]

    def test_marketing_website_call_and_copy_actions_use_tracked_urls(self):
        website_call = self._template(
            name="tracked_website_call",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "View offer",
                    "url": "https://example.com/offer",
                },
                {
                    "type": "call_phone",
                    "text": "Call sales",
                    "phone_number": "+918700000000",
                },
            ],
        )
        payload = template_service.build_meta_payload(template=website_call)
        buttons = self._buttons(payload)
        self.assertEqual([item["type"] for item in buttons], ["URL", "URL"])
        self.assertEqual(
            [item["url"] for item in buttons],
            [tracking_url_template(), tracking_url_template()],
        )
        self.assertNotIn("_shvya_track_cta", website_call.buttons[0])

        copy_code = self._template(
            name="tracked_copy_code",
            buttons=[
                {
                    "type": "copy_offer",
                    "text": "Copy offer",
                    "coupon_code": "SAVE20",
                }
            ],
        )
        copy_payload = template_service.build_meta_payload(template=copy_code)
        self.assertEqual(self._buttons(copy_payload)[0]["type"], "URL")
        self.assertEqual(
            self._buttons(copy_payload)[0]["url"],
            tracking_url_template(),
        )

    def test_authentication_copy_code_remains_native(self):
        template = self._template(
            name="authentication_code",
            category=WhatsAppTemplate.Category.AUTHENTICATION,
            buttons=[
                {
                    "type": "copy_offer",
                    "coupon_code": "123456",
                }
            ],
        )
        payload = template_service.build_meta_payload(template=template)
        self.assertEqual(self._buttons(payload)[0]["type"], "COPY_CODE")

    def test_tracking_url_parameter_is_hidden_from_sending_setup(self):
        template = self._template(
            name="tracked_field_hidden",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open",
                    "url": "https://example.com",
                }
            ],
        )
        payload = template_service.build_meta_payload(template=template)
        fields = campaign_policy.template_fields(
            {
                "components": payload["components"],
                "placeholder_mapping": {},
                "media_defaults": {},
            }
        )
        self.assertFalse(
            any(str(field["key"]).startswith("button") for field in fields)
        )

    def test_rejects_more_than_two_trackable_ctas(self):
        template = self._template(
            name="too_many_tracked_ctas",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Website",
                    "url": "https://example.com",
                },
                {
                    "type": "call_phone",
                    "text": "Call",
                    "phone_number": "+918700000000",
                },
                {
                    "type": "copy_offer",
                    "text": "Copy",
                    "coupon_code": "SAVE20",
                },
            ],
        )
        with self.assertRaisesRegex(
            template_service.TemplateError,
            "at most two Website, Call, or Copy Code",
        ):
            template_service.build_meta_payload(template=template)

    def test_send_time_creates_inactive_per_recipient_link_and_parameter(self):
        template = self._template(
            name="send_tracked_cta",
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-send-tracked-cta",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open offer",
                    "url": "https://example.com/offer",
                }
            ],
        )
        template_service.build_meta_payload(template=template)
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.phone_number_id,
            to_number="919876543210",
            body="Choose an action",
            status=WhatsAppMessage.Status.QUEUED,
            media_payload={
                "transport": "template",
                "template_id": str(template.id),
                "template_name": template.name,
            },
        )

        components, links = prepare_message_tracking(
            message=message,
            template=template,
            components=[],
        )

        self.assertEqual(len(links), 1)
        self.assertFalse(links[0].is_active)
        self.assertEqual(links[0].destination_url, "https://example.com/offer")
        self.assertEqual(
            components,
            [
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(links[0].token),
                        }
                    ],
                }
            ],
        )

    def _message_and_link(
        self,
        *,
        template,
        action_type,
        button_text,
        destination_url="",
        phone_number="",
        coupon_code="",
        token_path="button0",
    ):
        sent_at = timezone.now() - timedelta(minutes=5)
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.phone_number_id,
            to_number="919876543210",
            body="Choose an action",
            status=WhatsAppMessage.Status.DELIVERED,
            sent_at=sent_at,
            media_payload={
                "transport": "template",
                "template_id": str(template.id),
                "template_name": template.name,
            },
        )
        link = WhatsAppTemplateTrackedLink.objects.create(
            organization=self.organization,
            account=self.account,
            template=template,
            message=message,
            meta_template_id=template.meta_template_id,
            template_name=template.name,
            button_path=token_path,
            action_type=action_type,
            button_text=button_text,
            destination_url=destination_url,
            phone_number=phone_number,
            coupon_code=coupon_code,
            is_active=True,
            sent_at=sent_at,
        )
        return message, link

    def test_public_website_click_redirects_and_deduplicates_browser_retry(self):
        template = self._template(
            name="website_redirect",
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-website-redirect",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "Open offer",
                    "url": "https://example.com/offer",
                }
            ],
        )
        _, link = self._message_and_link(
            template=template,
            action_type=WhatsAppTemplateTrackedLink.ActionType.WEBSITE,
            button_text="Open offer",
            destination_url="https://example.com/offer",
        )
        url = reverse("whatsapp-template-tracked-cta", args=[link.token])

        response = self.client.get(url, HTTP_USER_AGENT="Mozilla/5.0")
        retry = self.client.get(url, HTTP_USER_AGENT="Mozilla/5.0")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://example.com/offer")
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(link.events.count(), 1)

    def test_public_call_and_copy_actions_are_real_counted_pages(self):
        template = self._template(
            name="call_copy_pages",
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-call-copy-pages",
            buttons=[],
        )
        _, call_link = self._message_and_link(
            template=template,
            action_type=WhatsAppTemplateTrackedLink.ActionType.CALL,
            button_text="Call sales",
            phone_number="+918700000000",
            token_path="button0",
        )
        _, copy_link = self._message_and_link(
            template=template,
            action_type=WhatsAppTemplateTrackedLink.ActionType.COPY_CODE,
            button_text="Copy offer",
            coupon_code="SAVE20",
            token_path="button1",
        )

        call_response = self.client.get(
            reverse("whatsapp-template-tracked-cta", args=[call_link.token]),
            HTTP_USER_AGENT="Mozilla/5.0",
        )
        copy_response = self.client.get(
            reverse("whatsapp-template-tracked-cta", args=[copy_link.token]),
            HTTP_USER_AGENT="Mozilla/5.0",
        )

        self.assertContains(call_response, "tel:+918700000000")
        self.assertContains(copy_response, "SAVE20")
        self.assertEqual(call_link.events.count(), 1)
        self.assertEqual(copy_link.events.count(), 1)

    def test_tracked_cta_analytics_reports_zero_and_real_clicks(self):
        template = self._template(
            name="tracked_analytics",
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-tracked-analytics",
            buttons=[
                {
                    "type": "visit_website",
                    "text": "View offer",
                    "url": "https://example.com/offer",
                }
            ],
        )
        template_service.build_meta_payload(template=template)
        _, link = self._message_and_link(
            template=template,
            action_type=WhatsAppTemplateTrackedLink.ActionType.WEBSITE,
            button_text="View offer",
            destination_url="https://example.com/offer",
        )
        today = timezone.localdate()
        base_result = {
            template.meta_template_id: {
                "template_id": template.meta_template_id,
                "days": [
                    {
                        "date": today.isoformat(),
                        "sent": 1,
                        "delivered": 1,
                        "read": 1,
                        "clicked": 0,
                    }
                ],
                "totals": {
                    "sent": 1,
                    "delivered": 1,
                    "read": 1,
                    "clicked": 0,
                },
                "rates": {
                    "delivered": 100.0,
                    "read": 100.0,
                    "clicked": None,
                },
                "availability": {"clicked": False},
                "clicks": [],
                "source": "shvya",
            }
        }

        zero = augment_tracked_cta_analytics(
            account=self.account,
            template_ids=[template.meta_template_id],
            start_date=today,
            end_date=today,
            results=base_result,
        )[template.meta_template_id]
        self.assertTrue(zero["availability"]["clicked"])
        self.assertEqual(zero["totals"]["clicked"], 0)

        for suffix in ("one", "two"):
            WhatsAppTemplateTrackedClick.objects.create(
                link=link,
                fingerprint=f"fingerprint-{suffix}",
                user_agent="Mozilla/5.0",
            )
        counted = augment_tracked_cta_analytics(
            account=self.account,
            template_ids=[template.meta_template_id],
            start_date=today,
            end_date=today,
            results=base_result,
        )[template.meta_template_id]
        self.assertEqual(counted["totals"]["clicked"], 2)
        self.assertEqual(counted["rates"]["clicked"], 200.0)
        self.assertEqual(counted["unique_click_total"], 1)
        self.assertEqual(
            counted["clicks"],
            [
                {
                    "type": "url_button",
                    "button_content": "View offer",
                    "count": 2,
                }
            ],
        )
