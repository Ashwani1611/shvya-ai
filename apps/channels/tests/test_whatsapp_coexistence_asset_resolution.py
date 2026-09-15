from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from services.channels.whatsapp_coexistence_asset_resolution import (
    _candidate_status_from_meta,
    _resolve_choice_with_meta_propagation,
    _select_unique_coexistence_choice,
)


class WhatsAppCoexistenceAssetResolutionTests(SimpleTestCase):
    def setUp(self):
        self.choices = [
            {
                "waba_id": "waba-1",
                "phone_number_id": "phone-cloud-only",
                "display_phone_number": "+911111111111",
            },
            {
                "waba_id": "waba-1",
                "phone_number_id": "phone-coexistence",
                "display_phone_number": "+922222222222",
            },
        ]

    def test_selects_unique_business_app_phone_from_multiple_authorized_numbers(self):
        status_getter = Mock(
            side_effect=lambda **kwargs: {
                "phone-cloud-only": {
                    "is_on_biz_app": False,
                    "platform_type": "CLOUD_API",
                },
                "phone-coexistence": {
                    "is_on_biz_app": True,
                    "platform_type": "CLOUD_API",
                },
            }[kwargs["phone_number_id"]]
        )

        selected = _select_unique_coexistence_choice(
            choices=self.choices,
            access_token="business-token",
            status_getter=status_getter,
        )

        self.assertEqual(selected["phone_number_id"], "phone-coexistence")
        self.assertEqual(status_getter.call_count, 2)

    def test_prefers_unique_cloud_api_candidate_when_multiple_numbers_are_on_business_app(self):
        status_getter = Mock(
            side_effect=lambda **kwargs: {
                "phone-cloud-only": {
                    "is_on_biz_app": True,
                    "platform_type": "ON_PREMISE",
                },
                "phone-coexistence": {
                    "is_on_biz_app": True,
                    "platform_type": "CLOUD_API",
                },
            }[kwargs["phone_number_id"]]
        )

        selected = _select_unique_coexistence_choice(
            choices=self.choices,
            access_token="business-token",
            status_getter=status_getter,
        )

        self.assertEqual(selected["phone_number_id"], "phone-coexistence")

    def test_accepts_unique_business_app_candidate_when_platform_transition_lags(self):
        status_getter = Mock(
            side_effect=lambda **kwargs: {
                "phone-cloud-only": {
                    "is_on_biz_app": False,
                    "platform_type": "CLOUD_API",
                },
                "phone-coexistence": {
                    "is_on_biz_app": True,
                    "platform_type": "ON_PREMISE",
                },
            }[kwargs["phone_number_id"]]
        )

        selected = _select_unique_coexistence_choice(
            choices=self.choices,
            access_token="business-token",
            status_getter=status_getter,
        )

        self.assertEqual(selected["phone_number_id"], "phone-coexistence")

    def test_accepts_string_true_and_smb_cloud_api_platform(self):
        status_getter = Mock(
            side_effect=lambda **kwargs: {
                "phone-cloud-only": {
                    "is_on_biz_app": False,
                    "platform_type": "CLOUD_API",
                },
                "phone-coexistence": {
                    "is_on_biz_app": "true",
                    "platform_type": "SMB_CLOUD_API",
                },
            }[kwargs["phone_number_id"]]
        )

        selected = _select_unique_coexistence_choice(
            choices=self.choices,
            access_token="business-token",
            status_getter=status_getter,
        )

        self.assertEqual(selected["phone_number_id"], "phone-coexistence")

    def test_retries_until_meta_exposes_selected_business_app_number(self):
        calls = {"count": 0}

        def status_getter(**kwargs):
            calls["count"] += 1
            second_pass = calls["count"] > len(self.choices)
            if kwargs["phone_number_id"] == "phone-coexistence" and second_pass:
                return {"is_on_biz_app": True, "platform_type": "CLOUD_API"}
            return {"is_on_biz_app": False, "platform_type": "CLOUD_API"}

        sleep_fn = Mock()
        selected = _resolve_choice_with_meta_propagation(
            choices=self.choices,
            access_token="business-token",
            status_getter=status_getter,
            retry_delays=(0.0, 0.25),
            sleep_fn=sleep_fn,
        )

        self.assertEqual(selected["phone_number_id"], "phone-coexistence")
        sleep_fn.assert_called_once_with(0.25)

    @patch("services.channels.whatsapp_coexistence_asset_resolution.requests.get")
    def test_candidate_status_falls_back_when_combined_graph_fields_fail(self, get):
        combined = Mock(ok=False, status_code=400)
        biz_app = Mock(ok=True, status_code=200)
        biz_app.json.return_value = {"is_on_biz_app": True, "id": "phone-1"}
        platform = Mock(ok=True, status_code=200)
        platform.json.return_value = {"platform_type": "CLOUD_API", "id": "phone-1"}
        get.side_effect = [combined, biz_app, platform]

        status = _candidate_status_from_meta(
            phone_number_id="phone-1",
            access_token="business-token",
        )

        self.assertIs(status["is_on_biz_app"], True)
        self.assertEqual(status["platform_type"], "CLOUD_API")
        self.assertEqual(get.call_count, 3)

    def test_does_not_guess_when_multiple_candidates_remain_ambiguous(self):
        status_getter = Mock(
            return_value={
                "is_on_biz_app": True,
                "platform_type": "CLOUD_API",
            }
        )

        selected = _select_unique_coexistence_choice(
            choices=self.choices,
            access_token="business-token",
            status_getter=status_getter,
        )

        self.assertIsNone(selected)

    def test_does_not_select_cloud_api_only_phone(self):
        status_getter = Mock(
            return_value={
                "is_on_biz_app": False,
                "platform_type": "CLOUD_API",
            }
        )

        selected = _select_unique_coexistence_choice(
            choices=self.choices,
            access_token="business-token",
            status_getter=status_getter,
        )

        self.assertIsNone(selected)
