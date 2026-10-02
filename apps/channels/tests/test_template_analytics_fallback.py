from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.organizations.models import Organization
from services.channels.template_analytics import fetch_template_analytics


class WhatsAppTemplateAnalyticsFallbackTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Template Receipt Org")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Template Receipt Business",
            phone_number_id="phone-template-receipts",
            waba_id="waba-template-receipts",
            access_token="template-receipts-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.template = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            name="receipt_offer",
            body="Hello",
            category=WhatsAppTemplate.Category.MARKETING,
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-receipt-offer",
        )

    @staticmethod
    def _response(*, ok, payload, status_code=200, text=""):
        response = Mock(ok=ok, status_code=status_code, text=text)
        response.json.return_value = payload
        return response

    def _template_payload(self):
        return {
            "transport": "template",
            "template_id": str(self.template.id),
            "template_name": self.template.name,
        }

    def _message(self, *, status, sent_at=None, media_payload=None, to_number="919876543210"):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number="15550000000",
            to_number=to_number,
            body="Template body",
            media_payload=(
                self._template_payload()
                if media_payload is None
                else media_payload
            ),
            status=status,
            sent_at=sent_at,
        )

    @patch("services.channels.template_analytics.meta.requests.post")
    @patch("services.channels.template_analytics.meta.requests.get")
    def test_enables_template_insights_after_meta_200007_and_retries(
        self,
        requests_get,
        requests_post,
    ):
        today = timezone.now().astimezone(dt_timezone.utc).date()
        start_ts = int(
            datetime.combine(
                today,
                datetime.min.time(),
                tzinfo=dt_timezone.utc,
            ).timestamp()
        )
        requests_get.side_effect = [
            self._response(
                ok=False,
                status_code=400,
                payload={
                    "error": {
                        "code": 100,
                        "error_subcode": 200007,
                        "message": (
                            "Template insights have not been enabled for this "
                            "WhatsApp Business account."
                        ),
                    }
                },
            ),
            self._response(
                ok=True,
                payload={
                    "template_analytics": {
                        "data": [
                            {
                                "template_id": self.template.meta_template_id,
                                "data_points": [
                                    {
                                        "start": start_ts,
                                        "sent": 4,
                                        "delivered": 3,
                                        "read": 2,
                                    }
                                ],
                            }
                        ]
                    }
                },
            ),
        ]
        requests_post.return_value = self._response(
            ok=True,
            payload={"success": True},
        )

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=today,
            end_date=today,
        )[self.template.meta_template_id]

        self.assertEqual(requests_get.call_count, 2)
        requests_post.assert_called_once()
        post_args, post_kwargs = requests_post.call_args
        self.assertEqual(
            post_args[0],
            "https://graph.facebook.com/v21.0/waba-template-receipts",
        )
        self.assertEqual(
            post_kwargs["params"],
            {"is_enabled_for_insights": "true"},
        )
        self.assertEqual(item["source"], "meta")
        self.assertEqual(item["totals"]["sent"], 4)
        self.assertEqual(item["totals"]["delivered"], 3)
        self.assertEqual(item["totals"]["read"], 2)

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_local_receipts_fill_meta_reporting_delay_without_double_counting(
        self,
        requests_get,
    ):
        sent_at = timezone.now()
        today = sent_at.astimezone(dt_timezone.utc).date()
        start_ts = int(
            datetime.combine(
                today,
                datetime.min.time(),
                tzinfo=dt_timezone.utc,
            ).timestamp()
        )
        for status in (
            WhatsAppMessage.Status.SENT,
            WhatsAppMessage.Status.DELIVERED,
            WhatsAppMessage.Status.READ,
        ):
            self._message(status=status, sent_at=sent_at)

        requests_get.return_value = self._response(
            ok=True,
            payload={
                "template_analytics": {
                    "data": [
                        {
                            "template_id": self.template.meta_template_id,
                            "data_points": [
                                {
                                    "start": start_ts,
                                    "sent": 1,
                                    "delivered": 1,
                                    "read": 0,
                                    "clicked": [
                                        {
                                            "type": "url",
                                            "button_content": "Open",
                                            "count": 2,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        )

        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=today,
            end_date=today,
        )[self.template.meta_template_id]

        self.assertEqual(item["source"], "meta+shvya")
        self.assertTrue(item["local_receipts_applied"])
        self.assertEqual(
            item["totals"],
            {"sent": 3, "delivered": 2, "read": 1, "clicked": 2},
        )
        self.assertEqual(item["rates"]["delivered"], 66.7)
        self.assertEqual(item["rates"]["read"], 50.0)
        self.assertEqual(item["rates"]["clicked"], 100.0)
        self.assertTrue(item["availability"]["clicked"])
        self.assertEqual(item["clicks"][0]["button_content"], "Open")

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_uses_local_receipts_when_meta_analytics_are_unavailable(
        self,
        requests_get,
    ):
        requests_get.return_value = self._response(
            ok=False,
            status_code=400,
            payload={
                "error": {
                    "code": 100,
                    "error_subcode": 200005,
                    "message": "Template analytics are unavailable.",
                }
            },
        )
        sent_at = timezone.now()
        for status in (
            WhatsAppMessage.Status.SENT,
            WhatsAppMessage.Status.DELIVERED,
            WhatsAppMessage.Status.READ,
            WhatsAppMessage.Status.FAILED,
        ):
            self._message(status=status, sent_at=sent_at)

        # Failed before Meta accepted the send: no sent_at, so it must not be
        # counted as sent. A queued template and an ordinary text message are
        # also excluded from template performance.
        self._message(
            status=WhatsAppMessage.Status.FAILED,
            to_number="919876543211",
        )
        self._message(
            status=WhatsAppMessage.Status.QUEUED,
            to_number="919876543212",
        )
        self._message(
            status=WhatsAppMessage.Status.READ,
            sent_at=sent_at,
            media_payload={},
            to_number="919876543213",
        )

        today = sent_at.astimezone(dt_timezone.utc).date()
        item = fetch_template_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=today,
            end_date=today,
        )[self.template.meta_template_id]

        self.assertEqual(item["source"], "shvya")
        self.assertEqual(item["source_label"], "SHVYA delivery receipts")
        self.assertEqual(
            item["totals"],
            {"sent": 4, "delivered": 2, "read": 1, "clicked": 0},
        )
        self.assertEqual(item["rates"]["delivered"], 50.0)
        self.assertEqual(item["rates"]["read"], 50.0)
        self.assertFalse(item["availability"]["clicked"])
        self.assertEqual(item["provider_error_code"], "200005")
