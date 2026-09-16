from unittest.mock import patch

from django.test import TestCase

from apps.channels.providers import whatsapp_embedded


class WhatsAppEmbeddedExchangeRegressionTests(TestCase):
    @patch("apps.channels.providers.whatsapp_embedded._get_json")
    def test_legacy_js_sdk_exchange_still_omits_redirect_without_context(self, get_json):
        get_json.return_value = {"access_token": "business-token"}

        whatsapp_embedded.exchange_code_for_access_token(
            app_id="123",
            app_secret="secret",
            code="code-2",
            redirect_uri="",
        )

        self.assertNotIn("redirect_uri", get_json.call_args.kwargs["params"])
