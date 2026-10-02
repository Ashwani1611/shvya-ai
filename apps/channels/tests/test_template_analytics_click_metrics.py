from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import TestCase

from apps.channels.models import WhatsAppAccount
from apps.organizations.models import Organization
from services.channels.template_analytics import fetch_template_analytics


class WhatsAppTemplateClickMetricTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Template Click Metrics"
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Click Metric Business",
            phone_number_id="phone-click-metrics",
            waba_id="waba-click-metrics",
            access_token="click-metrics-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.day = date(2026, 10, 2)
        self.start_ts = int(
            datetime(
                2026,
                10,
                2,
                tzinfo=dt_timezone.utc,
            ).timestamp()
        )

    def _response(self, clicked):
        response = Mock(ok=True, status_code=200, text="")
        response.json.return_value = {
            "template_analytics": {
                "data": [
                    {
                        "template_id": "meta-click-template",
                        "data_points": [
                            {
                                "start": self.start_ts,
                                "sent": 22,
                                "delivered": 20,
                                "read": 8,
                                "clicked": clicked,
                            }
                        ],
                    }
                ]
            }
        }
        return response

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_total_and_unique_rows_are_not_added_together(self, requests_get):
        requests_get.return_value = self._response(
            [
                {
                    "type": "url_button",
                    "button_content": "View offer",
                    "count": 5,
                },
                {
                    "type": "unique_url_button",
                    "button_content": "View offer",
                    "count": 3,
                },
            ]
        )

        item = fetch_template_analytics(
            account=self.account,
            template_ids=["meta-click-template"],
            start_date=self.day,
            end_date=self.day,
        )["meta-click-template"]

        self.assertEqual(item["totals"]["clicked"], 5)
        self.assertEqual(item["rates"]["clicked"], 25.0)
        self.assertEqual(item["click_count_basis"], "total")
        self.assertEqual(
            item["clicks"],
            [
                {
                    "type": "url_button",
                    "button_content": "View offer",
                    "count": 5,
                }
            ],
        )
        self.assertEqual(item["unique_click_total"], 3)
        self.assertEqual(item["unique_click_rate"], 15.0)
        self.assertEqual(
            item["unique_clicks"],
            [
                {
                    "type": "url_button",
                    "button_content": "View offer",
                    "count": 3,
                }
            ],
        )

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_unique_only_payload_is_used_as_an_explicit_fallback(
        self,
        requests_get,
    ):
        requests_get.return_value = self._response(
            [
                {
                    "type": "unique_quick_reply_button",
                    "button_content": "Book demo",
                    "count": 2,
                }
            ]
        )

        item = fetch_template_analytics(
            account=self.account,
            template_ids=["meta-click-template"],
            start_date=self.day,
            end_date=self.day,
        )["meta-click-template"]

        self.assertEqual(item["totals"]["clicked"], 2)
        self.assertEqual(item["click_count_basis"], "unique_fallback")
        self.assertEqual(item["unique_click_total"], 2)
        self.assertEqual(
            item["clicks"],
            [
                {
                    "type": "quick_reply_button",
                    "button_content": "Book demo",
                    "count": 2,
                }
            ],
        )
