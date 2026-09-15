from unittest.mock import patch

from django.test import TestCase

from apps.channels.models import WhatsAppAccount
from apps.organizations.models import Organization
from services.channels.whatsapp_coexistence_partner_runtime import (
    process_partner_coexistence_event,
)


class WhatsAppCoexistencePartnerEventTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Partner Sync Org")
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
            waba_id="waba-123",
            phone_number_id="phone-123",
            display_phone_number="+919999999999",
            business_name="Partner Sync Business",
            access_token="test-token",
        )

    @patch(
        "services.channels.whatsapp_coexistence_partner_runtime.handle_history_sync"
    )
    def test_top_level_history_event_activates_history_import(self, history_sync):
        history_sync.return_value = [object(), object()]
        data = {
            "id": "waba-123",
            "metadata": {
                "phone_number_id": "phone-123",
                "display_phone_number": "+919999999999",
            },
            "history": [],
        }

        handled = process_partner_coexistence_event(
            {"id": "delivery-1", "event": "history", "data": data}
        )

        self.assertTrue(handled)
        history_sync.assert_called_once_with(account=self.account, value=data)

    @patch(
        "services.channels.whatsapp_coexistence_partner_runtime.handle_smb_app_state_sync"
    )
    def test_top_level_app_state_event_activates_contact_sync(self, state_sync):
        state_sync.return_value = 2
        data = {
            "id": "waba-123",
            "metadata": {"phone_number_id": "phone-123"},
            "state_sync": [],
        }

        handled = process_partner_coexistence_event(
            {"id": "delivery-2", "event": "smb_app_state_sync", "data": data}
        )

        self.assertTrue(handled)
        state_sync.assert_called_once_with(account=self.account, value=data)

    @patch(
        "services.channels.whatsapp_coexistence_partner_runtime.handle_smb_message_echoes"
    )
    def test_top_level_message_echo_event_is_mirrored_into_inbox(self, echo_sync):
        echo_sync.return_value = [object()]
        data = {
            "id": "waba-123",
            "metadata": {"phone_number_id": "phone-123"},
            "message_echoes": [],
        }

        handled = process_partner_coexistence_event(
            {"id": "delivery-3", "event": "smb_message_echoes", "data": data}
        )

        self.assertTrue(handled)
        echo_sync.assert_called_once_with(account=self.account, value=data)

    def test_standard_cloud_api_payload_is_left_for_existing_runtime(self):
        handled = process_partner_coexistence_event(
            {"entry": [{"id": "waba-123", "changes": []}]}
        )

        self.assertFalse(handled)
