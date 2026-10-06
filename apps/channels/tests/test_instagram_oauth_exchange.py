"""Regression coverage for Meta Instagram Login response contracts."""

from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramOAuthAttempt
from apps.organizations.models import Organization
from services.channels import instagram_service as service


def meta_response(payload, *, status=200):
    response = Mock()
    response.ok = 200 <= status < 300
    response.status_code = status
    response.headers = {}
    response.text = ""
    response.json.return_value = payload
    return response


@override_settings(
    META_INSTAGRAM_APP_ID="instagram-app",
    META_INSTAGRAM_APP_SECRET="instagram-secret",
    META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS=True,
)
class InstagramOAuthContractTests(SimpleTestCase):
    @patch("services.channels.instagram_service.requests.request")
    def test_exchange_accepts_current_envelope_and_legacy_flat_response(self, request):
        details = {
            "access_token": "short-token",
            "user_id": "17840000000001",
            "permissions": list(service.INSTAGRAM_SCOPES),
        }
        redirect_uri = "https://shvya-ai.com/dashboard/instagram/connect/return/"
        for response in ({"data": [details]}, details):
            with self.subTest(wrapped="data" in response):
                request.reset_mock()
                request.side_effect = [
                    meta_response(response),
                    meta_response({"access_token": "long-token", "expires_in": 5_184_000}),
                ]
                token, expires_in, short_payload = service._exchange_authorization_code(
                    code="single-use-code", redirect_uri=redirect_uri,
                )
                self.assertEqual((token, expires_in), ("long-token", 5_184_000))
                self.assertEqual(short_payload["user_id"], "17840000000001")
                self.assertEqual(request.call_args_list[0].args, ("POST", service.OAUTH_TOKEN_URL))
                self.assertEqual(request.call_args_list[0].kwargs["data"], {
                    "client_id": "instagram-app", "client_secret": "instagram-secret",
                    "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                    "code": "single-use-code",
                })
                self.assertEqual(request.call_args_list[1].kwargs["params"]["access_token"], "short-token")

    @patch("services.channels.instagram_service.requests.request")
    def test_exchange_rejects_ambiguous_or_malformed_token_envelope(self, request):
        token = {"access_token": "short-token", "user_id": "17840000000001"}
        for payload in ({"data": []}, {"data": [token, token]}, {"data": ["invalid"]},
                        {"access_token": {"not": "a token"}}):
            with self.subTest(payload=payload):
                request.reset_mock()
                request.return_value = meta_response(payload)
                with self.assertRaisesMessage(service.InstagramAPIError, "did not return an access token"):
                    service._exchange_authorization_code(code="code", redirect_uri="https://example.com/callback/")
                self.assertEqual(request.call_count, 1)

    @patch("services.channels.instagram_service.requests.request")
    def test_missing_messaging_permission_is_actionable_before_token_upgrade(self, request):
        request.return_value = meta_response({"data": [{
            "access_token": "short-token", "user_id": "17840000000001",
            "permissions": ["instagram_business_basic"],
        }]})
        with self.assertRaisesMessage(service.InstagramAPIError, "approve both permissions"):
            service._exchange_authorization_code(code="code", redirect_uri="https://example.com/callback/")
        self.assertEqual(request.call_count, 1)

    @patch("services.channels.instagram_service.requests.request")
    def test_malformed_upgrade_never_persists_short_token_without_expiry(self, request):
        for upgrade in ({}, {"access_token": ""}, {"access_token": "token", "expires_in": "invalid"},
                        {"access_token": "token", "expires_in": 0}):
            with self.subTest(upgrade=upgrade):
                request.side_effect = [
                    meta_response({"access_token": "short-token", "user_id": "17840000000001"}),
                    meta_response(upgrade),
                ]
                with self.assertRaisesMessage(service.InstagramAPIError, "valid long-lived access token"):
                    service._exchange_authorization_code(code="code", redirect_uri="https://example.com/callback/")

    @patch("services.channels.instagram_service.requests.request")
    def test_malformed_token_response_is_not_echoed_in_error(self, request):
        request.side_effect = [
            meta_response({"access_token": "private-short-token", "user_id": "17840000000001"}),
            meta_response({"access_token": "private-long-token", "expires_in": "malformed",
                           "client_secret": "private-client-secret"}),
        ]
        with self.assertRaises(service.InstagramAPIError) as raised:
            service._exchange_authorization_code(code="private-code", redirect_uri="https://example.com/callback/")
        for secret in ("private-short-token", "private-long-token", "private-client-secret", "private-code"):
            self.assertNotIn(secret, str(raised.exception))

    @patch("services.channels.instagram_service._graph_get")
    def test_profile_supports_wrapped_and_flat_response_and_uses_professional_id(self, graph_get):
        details = {"id": "app-scoped-id", "user_id": "17840000000001", "username": "business"}
        for payload in (details, {"data": [details]}):
            with self.subTest(wrapped="data" in payload):
                graph_get.return_value = payload
                profile = service._fetch_profile("long-token", {"user_id": "login-id"})
                self.assertEqual(profile["resolved_ig_user_id"], "17840000000001")
                self.assertEqual(profile["username"], "business")

    @patch("services.channels.instagram_service._graph_get")
    def test_profile_can_use_token_user_id_but_never_app_scoped_id_alone(self, graph_get):
        graph_get.return_value = {"id": "app-scoped-id", "username": "business"}
        profile = service._fetch_profile("token", {"user_id": "17840000000001"})
        self.assertEqual(profile["resolved_ig_user_id"], "17840000000001")
        with self.assertRaisesMessage(service.InstagramAPIError, "profile ID was not returned"):
            service._fetch_profile("token")

    @patch("services.channels.instagram_service._graph_get")
    def test_profile_rejects_ambiguous_envelope_even_with_valid_token_identity(self, graph_get):
        graph_get.return_value = {"data": [{"user_id": "one"}, {"user_id": "two"}]}
        with self.assertRaisesMessage(service.InstagramAPIError, "profile ID was not returned"):
            service._fetch_profile("token", {"user_id": "one"})

    @patch("services.channels.instagram_service._graph_get")
    def test_profile_falls_back_to_documented_minimal_fields(self, graph_get):
        graph_get.side_effect = [
            service.InstagramAPIError("Unsupported optional field", status_code=400, code=100),
            {"data": [{"user_id": "17840000000001", "username": "business"}]},
        ]
        profile = service._fetch_profile("token")
        self.assertEqual(profile["resolved_ig_user_id"], "17840000000001")
        self.assertEqual(graph_get.call_args.kwargs["params"]["fields"], "user_id,username")

    @patch("services.channels.instagram_service._graph_get")
    def test_profile_does_not_retry_invalid_token_as_unsupported_field(self, graph_get):
        graph_get.side_effect = service.InstagramAPIError("Invalid token", status_code=400, code=190)
        with self.assertRaisesMessage(service.InstagramAPIError, "Invalid token"):
            service._fetch_profile("token")
        graph_get.assert_called_once()

    @patch("services.channels.instagram_service.requests.request")
    def test_network_error_does_not_expose_credentials_in_exception_url(self, request):
        request.side_effect = requests.ConnectionError(
            "https://graph.instagram.com/access_token?client_secret=secret-value&access_token=token-value"
        )
        with self.assertRaises(service.InstagramAPIError) as raised:
            service._request("GET", service.LONG_LIVED_TOKEN_URL, label="Token upgrade failed")
        self.assertTrue(raised.exception.transient)
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertNotIn("token-value", str(raised.exception))


@override_settings(META_INSTAGRAM_APP_ID="instagram-app", META_INSTAGRAM_APP_SECRET="instagram-secret")
class InstagramOAuthPersistenceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Instagram OAuth contract workspace")
        self.user = User.objects.create_user(
            email="instagram-contract@example.com", password="test-password",
            name="Workspace admin", organization=self.organization, role=User.Role.ADMIN,
        )
        self.attempt = InstagramOAuthAttempt.objects.create(
            organization=self.organization, created_by=self.user,
            authorization_code="single-use-code",
            redirect_uri="https://shvya-ai.com/dashboard/instagram/connect/return/",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def existing_account(self, ig_user_id="17840000000001"):
        return InstagramAccount.objects.create(
            organization=self.organization, ig_user_id=ig_user_id,
            access_token="old-token", status=InstagramAccount.Status.CONNECTED,
            webhook_subscribed=True, subscribed_fields=list(service.WEBHOOK_FIELDS),
            last_sync_at=timezone.now(), token_expires_at=timezone.now() + timedelta(days=2),
        )

    def oauth_responses(self, *, user_id="17840000000001", app_id="app-scoped-id"):
        return [
            meta_response({"data": [{"access_token": "short-token", "user_id": user_id,
                                     "permissions": list(service.INSTAGRAM_SCOPES)}]}),
            meta_response({"access_token": "long-token", "expires_in": 5_184_000}),
            meta_response({"data": [{"id": app_id, "user_id": user_id, "username": "business"}]}),
        ]

    @patch("services.channels.instagram_service.requests.request")
    def test_documented_response_envelopes_complete_connection_and_clear_code(self, request):
        request.side_effect = self.oauth_responses()
        account = service.complete_oauth_attempt(self.attempt)
        self.attempt.refresh_from_db()
        self.assertEqual(account.ig_user_id, "17840000000001")
        self.assertEqual(account.access_token, "long-token")
        self.assertEqual(account.username, "business")
        self.assertGreater(account.token_expires_at, timezone.now() + timedelta(days=59))
        self.assertEqual(self.attempt.status, InstagramOAuthAttempt.Status.CONNECTED)
        self.assertEqual(self.attempt.authorization_code, "")

    @patch("services.channels.instagram_service.requests.request")
    def test_reconnect_resets_subscription_and_sync_status(self, request):
        existing = self.existing_account()
        request.side_effect = self.oauth_responses()
        account = service.complete_oauth_attempt(self.attempt)
        self.assertEqual(account.pk, existing.pk)
        self.assertFalse(account.webhook_subscribed)
        self.assertEqual(account.subscribed_fields, [])
        self.assertIsNone(account.last_sync_at)

    @patch("services.channels.instagram_service.requests.request")
    def test_cannot_move_existing_history_to_another_instagram_account(self, request):
        account = self.existing_account()
        InstagramConversation.objects.create(
            organization=self.organization, account=account, participant_id="existing-customer",
        )
        request.side_effect = self.oauth_responses(user_id="17840000000002")
        with self.assertRaisesMessage(service.InstagramAPIError, "conversation history for another Instagram account"):
            service.complete_oauth_attempt(self.attempt)
        account.refresh_from_db()
        self.assertEqual(account.ig_user_id, "17840000000001")
        self.assertEqual(account.access_token, "old-token")

    @patch("services.channels.instagram_service.requests.request")
    def test_reconnect_can_correct_verified_app_scoped_identity_with_history(self, request):
        account = self.existing_account(ig_user_id="app-scoped-id")
        InstagramConversation.objects.create(
            organization=self.organization, account=account, participant_id="existing-customer",
        )
        request.side_effect = self.oauth_responses()
        reconnected = service.complete_oauth_attempt(self.attempt)
        self.assertEqual(reconnected.pk, account.pk)
        self.assertEqual(reconnected.ig_user_id, "17840000000001")
        self.assertEqual(reconnected.conversations.count(), 1)

    @patch("services.channels.instagram_service.requests.request")
    def test_verified_legacy_identity_cannot_be_reconnected_in_another_workspace(self, request):
        other = Organization.objects.create(name="Original Instagram workspace")
        InstagramAccount.objects.create(
            organization=other, ig_user_id="app-scoped-id", access_token="other-token",
            status=InstagramAccount.Status.CONNECTED,
        )
        request.side_effect = self.oauth_responses()
        with self.assertRaisesMessage(service.InstagramAPIError, "already connected to another SHVYA workspace"):
            service.complete_oauth_attempt(self.attempt)
        self.assertFalse(InstagramAccount.objects.filter(organization=self.organization).exists())

    @patch("services.channels.instagram_service.requests.request")
    def test_failed_refresh_keeps_previous_token_and_expiry(self, request):
        account = self.existing_account()
        old_expiry = account.token_expires_at
        request.return_value = meta_response({})
        with self.assertRaisesMessage(service.InstagramAPIError, "valid long-lived access token"):
            service.refresh_account_token(account)
        account.refresh_from_db()
        self.assertEqual(account.access_token, "old-token")
        self.assertEqual(account.token_expires_at, old_expiry)

    @patch("services.channels.instagram_service.requests.request")
    def test_webhook_subscription_requires_explicit_success(self, request):
        account = self.existing_account()
        for payload in ({}, {"success": False}, {"success": "false"}):
            with self.subTest(payload=payload):
                request.return_value = meta_response(payload)
                with self.assertRaisesMessage(service.InstagramAPIError, "did not confirm"):
                    service.subscribe_account_webhooks(account)
                account.refresh_from_db()
                self.assertFalse(account.webhook_subscribed)
                self.assertEqual(account.subscribed_fields, [])
