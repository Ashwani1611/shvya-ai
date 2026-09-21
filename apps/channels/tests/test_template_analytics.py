from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, TestCase

from apps.channels import template_ui
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.organizations.models import Organization
from services.channels.template_analytics import (
    TemplateAnalyticsError,
    fetch_template_analytics,
)


class MetaTemplateAnalyticsServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Template Analytics Org")
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            business_name="Analytics Business",
            phone_number_id="phone-analytics",
            waba_id="waba-analytics",
            access_token="analytics-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    @staticmethod
    def _response(payload):
        response = Mock(ok=True, status_code=200, text="")
        response.json.return_value = payload
        return response

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_maps_real_meta_daily_metrics_and_rates(self, requests_get):
        start_ts = int(
            datetime(2026, 9, 21, tzinfo=dt_timezone.utc).timestamp()
        )
        requests_get.return_value = self._response(
            {
                "template_analytics": {
                    "data": [
                        {
                            "granularity": "DAILY",
                            "template_id": "meta-offer",
                            "data_points": [
                                {
                                    "start": start_ts,
                                    "sent": 22,
                                    "delivered": 20,
                                    "read": 8,
                                    "clicked": [
                                        {
                                            "type": "url_button",
                                            "button_content": "View offer",
                                            "count": 3,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
        )

        data = fetch_template_analytics(
            account=self.account,
            template_ids=["meta-offer"],
            start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 21),
        )["meta-offer"]

        self.assertEqual(
            data["totals"],
            {"sent": 22, "delivered": 20, "read": 8, "clicked": 3},
        )
        self.assertEqual(data["rates"]["delivered"], 90.9)
        self.assertEqual(data["rates"]["read"], 40.0)
        self.assertEqual(data["rates"]["clicked"], 15.0)
        self.assertTrue(data["availability"]["clicked"])
        self.assertEqual(
            data["clicks"],
            [{"type": "url_button", "button_content": "View offer", "count": 3}],
        )
        self.assertEqual(data["days"][0]["date"], "2026-09-21")

        args, kwargs = requests_get.call_args
        self.assertEqual(
            args[0],
            "https://graph.facebook.com/v21.0/waba-analytics/template_analytics",
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer analytics-token",
        )
        self.assertEqual(kwargs["params"]["granularity"], "DAILY")
        self.assertEqual(kwargs["params"]["template_ids"], ["meta-offer"])
        self.assertEqual(
            kwargs["params"]["metric_types"],
            ["SENT", "DELIVERED", "READ", "CLICKED"],
        )
        self.assertLess(kwargs["params"]["start"], kwargs["params"]["end"])

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_missing_click_metric_is_unavailable_not_fabricated(self, requests_get):
        start_ts = int(
            datetime(2026, 9, 20, tzinfo=dt_timezone.utc).timestamp()
        )
        requests_get.return_value = self._response(
            {
                "template_analytics": {
                    "data": [
                        {
                            "template_id": "meta-no-click",
                            "data_points": [
                                {
                                    "start": start_ts,
                                    "sent": 5,
                                    "delivered": 4,
                                    "read": 2,
                                }
                            ]
                        }
                    ]
                }
            }
        )

        data = fetch_template_analytics(
            account=self.account,
            template_ids=["meta-no-click"],
            start_date=date(2026, 9, 20),
            end_date=date(2026, 9, 20),
        )["meta-no-click"]

        self.assertFalse(data["availability"]["clicked"])
        self.assertEqual(data["totals"]["clicked"], 0)
        self.assertEqual(data["clicks"], [])
        self.assertEqual(data["rates"]["clicked"], 0.0)

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_zero_denominators_do_not_create_fake_percentages(self, requests_get):
        start_ts = int(
            datetime(2026, 9, 19, tzinfo=dt_timezone.utc).timestamp()
        )
        requests_get.return_value = self._response(
            {
                "template_analytics": {
                    "data": [
                        {
                            "template_id": "meta-zero",
                            "data_points": [
                                {
                                    "start": start_ts,
                                    "sent": 0,
                                    "delivered": 0,
                                    "read": 0,
                                    "clicked": [],
                                }
                            ]
                        }
                    ]
                }
            }
        )

        data = fetch_template_analytics(
            account=self.account,
            template_ids=["meta-zero"],
            start_date=date(2026, 9, 19),
            end_date=date(2026, 9, 19),
        )["meta-zero"]

        self.assertIsNone(data["rates"]["delivered"])
        self.assertIsNone(data["rates"]["read"])
        self.assertIsNone(data["rates"]["clicked"])
        self.assertTrue(data["availability"]["clicked"])

    @patch("services.channels.template_analytics.meta.requests.get")
    def test_batches_more_than_ten_meta_template_ids(self, requests_get):
        requests_get.return_value = self._response(
            {"template_analytics": {"data": []}}
        )
        ids = [f"meta-{index}" for index in range(11)]

        data = fetch_template_analytics(
            account=self.account,
            template_ids=ids,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 7),
        )

        self.assertEqual(len(data), 11)
        self.assertEqual(requests_get.call_count, 2)

    def test_rejects_ranges_longer_than_ninety_days(self):
        with self.assertRaisesRegex(
            TemplateAnalyticsError,
            "maximum 90-day range",
        ):
            fetch_template_analytics(
                account=self.account,
                template_ids=["meta-one"],
                start_date=date(2026, 6, 1),
                end_date=date(2026, 9, 21),
            )


class WhatsAppTemplateAnalyticsViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.org = Organization.objects.create(name="Analytics View Org")
        self.other_org = Organization.objects.create(name="Other Analytics Org")
        self.user = SimpleNamespace(organization=self.org)
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            business_name="View Business",
            phone_number_id="phone-view",
            waba_id="waba-view",
            access_token="view-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.other_account = WhatsAppAccount.objects.create(
            organization=self.other_org,
            business_name="Other View Business",
            phone_number_id="phone-other",
            waba_id="waba-other",
            access_token="other-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=self.account,
            name="wome_offer",
            body="Hello",
            category=WhatsAppTemplate.Category.MARKETING,
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-view",
        )
        WhatsAppTemplateMetadata.objects.create(
            template=self.template,
            language="en_US",
        )
        self.other_template = WhatsAppTemplate.objects.create(
            organization=self.other_org,
            account=self.other_account,
            name="other_offer",
            body="Hello",
            category=WhatsAppTemplate.Category.MARKETING,
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-other",
        )

    @patch("apps.channels.template_ui.timezone.localdate")
    def test_range_presets_are_inclusive(self, localdate):
        localdate.return_value = date(2026, 9, 21)
        request = self.factory.get("/dashboard/whatsapp/templates/x/analytics/?range=7")

        start, end, value = template_ui._template_analytics_range(request)

        self.assertEqual(value, "7")
        self.assertEqual(start, date(2026, 9, 15))
        self.assertEqual(end, date(2026, 9, 21))

    @patch("apps.channels.template_ui.fetch_template_analytics")
    def test_detail_endpoint_returns_meta_values_and_template_metadata(self, fetch):
        fetch.return_value = {
            "meta-view": {
                "template_id": "meta-view",
                "days": [
                    {
                        "date": "2026-09-21",
                        "sent": 22,
                        "delivered": 20,
                        "read": 8,
                        "clicked": 0,
                    }
                ],
                "totals": {
                    "sent": 22,
                    "delivered": 20,
                    "read": 8,
                    "clicked": 0,
                },
                "rates": {
                    "delivered": 90.9,
                    "read": 40.0,
                    "clicked": 0.0,
                },
                "availability": {"clicked": True},
                "clicks": [],
                "fetched_at": "2026-09-21T02:16:00+00:00",
            }
        }
        request = self.factory.get(
            f"/dashboard/whatsapp/templates/{self.template.id}/analytics/?range=7"
        )
        request.crm_user = self.user

        response = template_ui.template_analytics.__wrapped__(
            request,
            self.template.id,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["template"]["name"], "wome_offer")
        self.assertEqual(payload["template"]["language"], "en_US")
        self.assertEqual(payload["analytics"]["totals"]["delivered"], 20)
        self.assertEqual(payload["analytics"]["rates"]["read"], 40.0)
        self.assertIn("read receipts", payload["read_receipt_note"])

        self.assertEqual(fetch.call_count, 1)
        kwargs = fetch.call_args.kwargs
        self.assertEqual(kwargs["account"].id, self.account.id)
        self.assertEqual(kwargs["template_ids"], ["meta-view"])

    @patch("apps.channels.template_ui.fetch_template_analytics")
    def test_detail_endpoint_cannot_access_other_organization_template(self, fetch):
        request = self.factory.get(
            f"/dashboard/whatsapp/templates/{self.other_template.id}/analytics/?range=7"
        )
        request.crm_user = self.user

        response = template_ui.template_analytics.__wrapped__(
            request,
            self.other_template.id,
        )

        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()

    @patch("apps.channels.template_ui.fetch_template_analytics")
    @patch("apps.channels.template_ui.timezone.localdate")
    def test_summary_meta_failure_stays_200_and_does_not_break_template_page(
        self,
        localdate,
        fetch,
    ):
        localdate.return_value = date(2026, 9, 21)
        fetch.side_effect = TemplateAnalyticsError(
            "Template analytics is not available for this WABA.",
            status_code=403,
            meta_error_code="100",
        )
        request = self.factory.get(
            "/dashboard/whatsapp/templates/analytics-summary/"
        )
        request.crm_user = self.user

        response = template_ui.template_analytics_summary.__wrapped__(request)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["templates"], {})
        self.assertEqual(
            payload["warnings"],
            ["Template analytics is not available for this WABA."],
        )
