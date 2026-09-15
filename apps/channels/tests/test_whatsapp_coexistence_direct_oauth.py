import json
from types import SimpleNamespace
from unittest.mock import ANY, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels import coexistence_oauth_ui
from apps.channels.connection_attempts import WhatsAppConnectionAttempt
from apps.channels.providers import whatsapp_embedded
from apps.organizations.models import Organization


class WhatsAppCoexistenceDirectOAuthTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Direct Coexistence Org")
        self.user = User.objects.create_user(
            email="direct-coexistence@example.com",
            password="test-password",
            name="Direct Coexistence Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    @override_settings(
        META_APP_ID="123456",
        META_APP_SECRET="meta-secret",
        META_WA_EMBEDDED_SIGNUP_CONFIG_ID="config-123",
    )
    def test_coexistence_page_replaces_js_sdk_launcher_with_direct_oauth(self):
        response = self.client.get(reverse("whatsapp-connect-coexistence"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-shvya-coexistence-direct-oauth")
        self.assertContains(response, reverse("whatsapp-coexistence-direct-start"))
        self.assertEqual(
            response["Cache-Control"],
            "no-store, no-cache, must-revalidate, max-age=0",
        )
        self.assertEqual(response["Pragma"], "no-cache")

    @override_settings(
        META_APP_ID="123456",
        META_APP_SECRET="meta-secret",
        META_WA_EMBEDDED_SIGNUP_CONFIG_ID="config-123",
    )
    def test_direct_start_requests_code_with_coexistence_extras_and_no_scope(self):
        response = self.client.get(reverse("whatsapp-coexistence-direct-start"))

        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response["Location"])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.facebook.com")
        self.assertEqual(params["client_id"], ["123456"])
        self.assertEqual(params["config_id"], ["config-123"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["override_default_response_type"], ["true"])
        self.assertEqual(params["auth_type"], ["rerequest"])
        self.assertNotIn("scope", params)

        extras = json.loads(params["extras"][0])
        self.assertEqual(extras["featureType"], "whatsapp_business_app_onboarding")
        self.assertEqual(extras["sessionInfoVersion"], "3")
        self.assertEqual(extras["setup"], {})

        expected_redirect = (
            "http://testserver" + reverse("whatsapp-embedded-signup-direct-return")
        )
        self.assertEqual(params["redirect_uri"], [expected_redirect])

        marker = self.client.session[coexistence_oauth_ui._SESSION_KEY]
        self.assertEqual(marker["organization_id"], str(self.org.id))
        self.assertEqual(marker["state"], params["state"][0])
        self.assertEqual(marker["redirect_uri"], expected_redirect)
        attempt = WhatsAppConnectionAttempt.objects.get(id=marker["attempt_id"])
        self.assertEqual(attempt.stage, "coexistence_direct_oauth_started")

    @override_settings(
        META_APP_ID="123456",
        META_APP_SECRET="meta-secret",
        META_WA_EMBEDDED_SIGNUP_CONFIG_ID="config-123",
    )
    @patch("apps.channels.coexistence_oauth_ui.complete_coexistence_signup")
    def test_shared_direct_return_dispatches_coexistence_and_uses_oauth_code(
        self,
        complete_signup,
    ):
        account = SimpleNamespace(id="account-1")
        complete_signup.return_value = (account, "", {})

        start = self.client.get(reverse("whatsapp-coexistence-direct-start"))
        params = parse_qs(urlparse(start["Location"]).query)
        state = params["state"][0]

        response = self.client.get(
            reverse("whatsapp-embedded-signup-direct-return"),
            {"code": "oauth-code", "state": state},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('whatsapp-accounts')}?connected=account-1",
        )
        complete_signup.assert_called_once_with(
            organization=self.org,
            code="oauth-code",
            attempt=ANY,
        )
        attempt = complete_signup.call_args.kwargs["attempt"]
        attempt.refresh_from_db()
        self.assertTrue(attempt.code_received)
        self.assertEqual(attempt.stage, "coexistence_direct_oauth_code_received")
        self.assertNotIn(coexistence_oauth_ui._SESSION_KEY, self.client.session)

    @override_settings(
        META_APP_ID="123456",
        META_APP_SECRET="meta-secret",
        META_WA_EMBEDDED_SIGNUP_CONFIG_ID="config-123",
    )
    @patch(
        "apps.channels.coexistence_oauth_ui.embedded_oauth_ui.whatsapp_embedded_signup_direct_return_view"
    )
    def test_shared_return_preserves_normal_connect_api_without_coexistence_session(
        self,
        standard_return,
    ):
        standard_return.return_value = HttpResponse("standard-direct-return")

        response = self.client.get(reverse("whatsapp-embedded-signup-direct-return"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"standard-direct-return")
        standard_return.assert_called_once()


class WhatsAppEmbeddedRedirectContextTests(TestCase):
    @patch("apps.channels.providers.whatsapp_embedded._get_json")
    def test_context_redirect_uri_is_repeated_during_token_exchange(self, get_json):
        get_json.return_value = {"access_token": "business-token"}
        callback = "https://dashboard.example.test/dashboard/whatsapp/connect/api/direct/return/"

        with whatsapp_embedded.oauth_redirect_uri(callback):
            token = whatsapp_embedded.exchange_code_for_access_token(
                app_id="123",
                app_secret="secret",
                code="code-1",
                redirect_uri="",
            )

        self.assertEqual(token, "business-token")
        self.assertEqual(get_json.call_args.kwargs["params"]["redirect_uri"], callback)

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
