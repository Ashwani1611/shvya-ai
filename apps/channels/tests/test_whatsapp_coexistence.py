from unittest.mock import call, patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_coexistence_service import (
    complete_coexistence_signup,
    process_coexistence_webhook_payload,
)


class WhatsAppCoexistenceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Coexistence Org")
        self.user = User.objects.create_user(
            email="coexistence@example.com",
            password="test-password",
            name="Coexistence Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="WhatsApp Sales",
            country_code="+91",
            phone_number="918700274739",
            owner=self.user,
        )
        self.stage, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline,
            display_order=1,
            defaults={"name": "New"},
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
    def test_connect_api_coexistence_card_routes_to_dedicated_flow(self):
        response = self.client.get(f"{reverse('whatsapp-connect-api')}?add=1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("whatsapp-connect-coexistence"))

        coexistence = self.client.get(reverse("whatsapp-connect-coexistence"))
        self.assertEqual(coexistence.status_code, 200)
        self.assertContains(coexistence, "whatsapp_business_app_onboarding")
        self.assertContains(coexistence, "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING")
        self.assertContains(coexistence, reverse("whatsapp-coexistence-callback"))

    @override_settings(META_APP_ID="123456", META_APP_SECRET="meta-secret")
    @patch("services.channels.whatsapp_coexistence_service.request_smb_app_data_sync")
    @patch("services.channels.whatsapp_coexistence_service.whatsapp_provider.subscribe_app_to_waba")
    @patch("services.channels.whatsapp_coexistence_service._coexistence_status")
    @patch("services.channels.whatsapp_coexistence_service._phone_details_without_registration")
    @patch("services.channels.whatsapp_coexistence_service._resolve_signup_assets")
    @patch("services.channels.whatsapp_coexistence_service.embedded_provider.exchange_code_for_access_token")
    @patch("services.channels.whatsapp_phone_registration.register_phone_number")
    def test_coexistence_completion_skips_phone_register_and_uses_api_transport(
        self,
        register_phone,
        exchange_code,
        resolve_assets,
        phone_details,
        coexistence_status,
        subscribe,
        request_sync,
    ):
        exchange_code.return_value = "business-token"
        resolve_assets.return_value = ("waba-1", "phone-id-1")
        phone_details.return_value = {
            "display_phone_number": "+918700274739",
            "verified_name": "Coexistence Business",
        }
        coexistence_status.return_value = {
            "is_on_biz_app": True,
            "platform_type": "CLOUD_API",
        }
        request_sync.side_effect = [
            {"request_id": "contacts-request"},
            {"request_id": "history-request"},
        ]

        account, warning, sync_results = complete_coexistence_signup(
            organization=self.org,
            code="oauth-code",
            waba_id="waba-1",
            phone_number_id="phone-id-1",
        )

        register_phone.assert_not_called()
        subscribe.assert_called_once_with(
            waba_id="waba-1",
            access_token="business-token",
        )
        self.assertEqual(
            request_sync.call_args_list,
            [
                call(
                    phone_number_id="phone-id-1",
                    access_token="business-token",
                    sync_type="smb_app_state_sync",
                ),
                call(
                    phone_number_id="phone-id-1",
                    access_token="business-token",
                    sync_type="history",
                ),
            ],
        )
        self.assertEqual(warning, "")
        self.assertTrue(sync_results["smb_app_state_sync"])
        self.assertTrue(sync_results["history"])
        self.assertEqual(account.connection_type, WhatsAppAccount.ConnectionType.API)
        self.assertEqual(account.status, WhatsAppAccount.Status.CONNECTED)
        self.assertTrue(account.is_active)

    @patch("services.channels.realtime.queue_message_publish")
    @patch("services.channels.whatsapp_service._queue_whatsapp_engagement")
    def test_business_app_echo_is_mirrored_as_outbound_without_waking_ai(
        self,
        engage,
        publish,
    ):
        account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            waba_id="waba-1",
            phone_number_id="phone-id-1",
            display_phone_number="+918700274739",
            access_token="token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [
                        {
                            "field": "smb_message_echoes",
                            "value": {
                                "metadata": {
                                    "display_phone_number": "918700274739",
                                    "phone_number_id": "phone-id-1",
                                },
                                "message_echoes": [
                                    {
                                        "from": "918700274739",
                                        "to": "919999999999",
                                        "id": "wamid.echo-1",
                                        "timestamp": "1778216475",
                                        "type": "text",
                                        "text": {"body": "Reply sent from the Business App"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        process_coexistence_webhook_payload(payload)

        message = WhatsAppMessage.objects.get(external_id="wamid.echo-1")
        self.assertEqual(message.account, account)
        self.assertEqual(message.direction, WhatsAppMessage.Direction.OUTBOUND)
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)
        self.assertEqual(message.body, "Reply sent from the Business App")
        self.assertTrue(message.is_read)
        self.assertIsNotNone(message.lead)
        engage.assert_not_called()
        publish.assert_called_once()
