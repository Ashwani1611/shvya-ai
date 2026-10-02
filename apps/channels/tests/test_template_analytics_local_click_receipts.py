from datetime import datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.organizations.models import Organization
from services.channels.template_analytics import fetch_template_analytics
from services.channels.template_click_receipts import extract_quick_reply_receipt


class WhatsAppTemplateLocalClickReceiptTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Local Click Receipts")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Click Receipt Business",
            phone_number_id="phone-local-clicks",
            waba_id="waba-local-clicks",
            access_token="local-clicks-token",
            display_phone_number="+911111111111",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.template = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            name="quick_reply_offer",
            category=WhatsAppTemplate.Category.MARKETING,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Would you like a demo?",
            buttons=[
                {"type": "text_back", "text": "Book demo"},
                {"type": "text_back", "text": "Not now"},
            ],
            meta_template_id="meta-quick-reply-offer",
        )
        self.sent_at = timezone.now() - timedelta(hours=1)
        self.day = self.sent_at.astimezone(dt_timezone.utc).date()
        self.start_ts = int(
            datetime.combine(
                self.day,
                time.min,
                tzinfo=dt_timezone.utc,
            ).timestamp()
        )

    def _meta_response(self, *, clicked_marker=False, clicked=None):
        point = {
            "start": self.start_ts,
            "sent": 1,
            "delivered": 1,
            "read": 1,
        }
        if clicked_marker:
            point["clicked"] = clicked if clicked is not None else []
        response = Mock(ok=True, status_code=200, text="")
        response.json.return_value = {
            "template_analytics": {
                "data": [
                    {
                        "template_id": self.template.meta_template_id,
                        "data_points": [point],
                    }
                ]
            }
        }
        return response

    def _outbound(self, *, external_id="wamid.outbound.1", recipient="919876543210"):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            external_id=external_id,
            from_number="911111111111",
            to_number=recipient,
            body="Would you like a demo?",
            message_type=WhatsAppMessage.MessageType.TEXT,
            media_payload={
                "transport": "template",
                "template_id": str(self.template.id),
                "template_name": self.template.name,
                "template_display": {
                    "buttons": [
                        {"type": "text_back", "text": "Book demo", "detail": ""},
                        {"type": "text_back", "text": "Not now", "detail": ""},
                    ]
                },
            },
            status=WhatsAppMessage.Status.DELIVERED,
            sent_at=self.sent_at,
        )

    def _quick_reply(
        self,
        *,
        context_id="wamid.outbound.1",
        external_id="wamid.inbound.1",
        sender="919876543210",
        label="Book demo",
    ):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=external_id,
            from_number=sender,
            to_number="911111111111",
            body=label,
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={
                "id": external_id,
                "from": sender,
                "type": "button",
                "context": {"id": context_id},
                "button": {"payload": "book_demo", "text": label},
            },
            is_read=False,
        )

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_uses_quick_reply_receipt_when_meta_omits_clicked_field(
        self,
        requests_get,
    ):
        self._outbound()
        self._quick_reply()
        requests_get.return_value = self._meta_response()

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
        )[self.template.meta_template_id]

        self.assertTrue(item["availability"]["clicked"])
        self.assertEqual(item["totals"]["clicked"], 1)
        self.assertEqual(item["rates"]["clicked"], 100.0)
        self.assertEqual(item["click_count_basis"], "local_quick_reply")
        self.assertEqual(item["click_source"], "shvya_quick_reply_receipts")
        self.assertEqual(
            item["clicks"],
            [
                {
                    "type": "quick_reply_button",
                    "button_content": "Book demo",
                    "count": 1,
                }
            ],
        )

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_local_receipt_fills_empty_meta_clicked_array(self, requests_get):
        self._outbound()
        self._quick_reply()
        requests_get.return_value = self._meta_response(
            clicked_marker=True,
            clicked=[],
        )

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
        )[self.template.meta_template_id]

        self.assertTrue(item["availability"]["clicked"])
        self.assertEqual(item["totals"]["clicked"], 1)
        self.assertEqual(
            item["click_count_basis"],
            "meta_with_local_quick_reply_floor",
        )
        self.assertEqual(item["click_source"], "meta+shvya")

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_quick_reply_template_with_no_clicks_reports_zero_not_unavailable(
        self,
        requests_get,
    ):
        self._outbound()
        requests_get.return_value = self._meta_response()

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
        )[self.template.meta_template_id]

        self.assertTrue(item["availability"]["clicked"])
        self.assertEqual(item["totals"]["clicked"], 0)
        self.assertEqual(item["clicks"], [])
        self.assertEqual(item["click_count_basis"], "local_quick_reply")

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_total_and_unique_local_clicks_are_kept_separate(self, requests_get):
        self._outbound(external_id="wamid.outbound.1", recipient="919876543210")
        self._outbound(external_id="wamid.outbound.2", recipient="919876543210")
        self._outbound(external_id="wamid.outbound.3", recipient="919999999999")
        self._quick_reply(
            context_id="wamid.outbound.1",
            external_id="wamid.inbound.1",
            sender="919876543210",
        )
        self._quick_reply(
            context_id="wamid.outbound.2",
            external_id="wamid.inbound.2",
            sender="919876543210",
        )
        self._quick_reply(
            context_id="wamid.outbound.3",
            external_id="wamid.inbound.3",
            sender="919999999999",
        )
        response = self._meta_response()
        response.json.return_value["template_analytics"]["data"][0]["data_points"][0].update(
            {"sent": 3, "delivered": 3, "read": 3}
        )
        requests_get.return_value = response

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
        )[self.template.meta_template_id]

        self.assertEqual(item["totals"]["clicked"], 3)
        self.assertEqual(item["unique_click_total"], 2)
        self.assertEqual(item["rates"]["clicked"], 100.0)
        self.assertEqual(item["unique_click_rate"], 66.7)

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_unmatched_context_is_not_counted(self, requests_get):
        self._outbound()
        self._quick_reply(context_id="wamid.unknown")
        requests_get.return_value = self._meta_response()

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
        )[self.template.meta_template_id]

        self.assertEqual(item["totals"]["clicked"], 0)
        self.assertEqual(item["clicks"], [])

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_url_only_template_does_not_fabricate_local_clicks(self, requests_get):
        url_button = {
            "type": "visit_website",
            "text": "Open offer",
            "url": "https://example.com/offer",
        }
        self.template.buttons = [url_button]
        self.template.save(update_fields=["buttons", "updated_at"])
        outbound = self._outbound()
        payload = dict(outbound.media_payload)
        payload["template_display"] = {
            "buttons": [
                {
                    "type": "visit_website",
                    "text": "Open offer",
                    "detail": "https://example.com/offer",
                }
            ]
        }
        outbound.media_payload = payload
        outbound.save(update_fields=["media_payload", "updated_at"])
        self._quick_reply()
        requests_get.return_value = self._meta_response()

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
        )[self.template.meta_template_id]

        self.assertFalse(item["availability"]["clicked"])
        self.assertEqual(item["totals"]["clicked"], 0)


class QuickReplyReceiptExtractionTests(TestCase):
    def test_interactive_button_reply_keeps_context_and_visible_label(self):
        receipt = extract_quick_reply_receipt(
            {
                "type": "interactive",
                "context": {"id": "wamid.outbound.interactive"},
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {
                        "id": "rebook_demo",
                        "title": "Rebook demo",
                    },
                },
            }
        )

        self.assertEqual(
            receipt,
            {
                "source_message_id": "wamid.outbound.interactive",
                "button_type": "quick_reply_button",
                "button_content": "Rebook demo",
            },
        )

    def test_reply_without_context_is_not_attributed(self):
        self.assertIsNone(
            extract_quick_reply_receipt(
                {
                    "type": "button",
                    "button": {"payload": "book_demo", "text": "Book demo"},
                }
            )
        )
