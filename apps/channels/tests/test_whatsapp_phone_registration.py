from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from apps.channels.providers import whatsapp as whatsapp_provider
from services.channels.whatsapp_phone_registration import (
    _managed_registration_pin,
    register_phone_number,
)


@override_settings(SECRET_KEY="registration-test-secret")
class WhatsAppPhoneRegistrationTests(SimpleTestCase):
    def test_managed_pin_is_stable_six_digit_and_phone_scoped(self):
        first = _managed_registration_pin("111111111")
        again = _managed_registration_pin("111111111")
        other = _managed_registration_pin("222222222")

        self.assertEqual(first, again)
        self.assertRegex(first, r"^\d{6}$")
        self.assertNotEqual(first, other)

    @patch("services.channels.whatsapp_phone_registration.requests.post")
    def test_register_phone_calls_meta_register_endpoint(self, post):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = '{"success":true}'
        response.json.return_value = {"success": True}
        post.return_value = response

        result = register_phone_number(
            phone_number_id="1234567890",
            access_token="secret-token",
        )

        self.assertEqual(result, {"success": True})
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            f"{whatsapp_provider.GRAPH_API_BASE}/1234567890/register",
        )
        self.assertEqual(kwargs["json"]["messaging_product"], "whatsapp")
        self.assertRegex(kwargs["json"]["pin"], r"^\d{6}$")
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer secret-token",
        )

    @patch("services.channels.whatsapp_phone_registration.requests.post")
    def test_registration_failure_preserves_meta_response_for_diagnostics(self, post):
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.text = '{"error":{"code":100,"message":"registration failed"}}'
        post.return_value = response

        with self.assertRaises(whatsapp_provider.WhatsAppAPIError) as raised:
            register_phone_number(
                phone_number_id="1234567890",
                access_token="secret-token",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("registration failed", raised.exception.response_body)
