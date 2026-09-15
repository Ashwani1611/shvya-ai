from unittest.mock import Mock

from django.test import SimpleTestCase

from services.channels.whatsapp_coexistence_asset_resolution import (
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
