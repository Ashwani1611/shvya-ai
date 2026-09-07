from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Pipeline
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import create_hosted_account


class HostedWhatsAppRecoveryTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Hosted Recovery Org")
        self.user = User.objects.create_user(
            email="hosted-recovery@example.com",
            password="test-password",
            name="Hosted Recovery Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        Pipeline.objects.create(
            organization=self.org,
            name="Hosted Recovery Sales",
            country_code="+91",
            phone_number="8700274739",
            owner=self.user,
        )
        self.account, _pipeline, _created = create_hosted_account(
            organization=self.org,
            created_by=self.user,
            country_code="+91",
            phone_number="8700274739",
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    @patch("apps.channels.hosted_ui.WhatsAppWebClient.get_session")
    def test_status_endpoint_reconciles_running_gateway_to_connected(self, get_session):
        get_session.return_value = {
            "status": "running",
            "phoneNumber": "+918700274739",
        }

        response = self.client.get(
            reverse("whatsapp-hosted-session-status", args=[self.account.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "running")
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED)

    @patch("apps.channels.hosted_ui.sync_hosted_history_task.delay")
    def test_empty_chat_page_queues_history_recovery(self, delay):
        response = self.client.get(
            reverse("whatsapp-hosted-session-chats", args=[self.account.id])
        )

        self.assertEqual(response.status_code, 200)
        delay.assert_called_once_with(str(self.account.id))
        self.assertTrue(response.context["sync_requested"])

    @patch("apps.channels.hosted_ui.sync_hosted_history_task.delay")
    def test_chat_page_does_not_resync_when_messages_exist(self, delay):
        from apps.channels.models import WhatsAppMessage

        WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            direction=WhatsAppMessage.Direction.INBOUND,
            from_number="+919811112222",
            to_number="+918700274739",
            body="Existing message",
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.RECEIVED,
            external_id="wweb:existing-recovery-message",
        )

        response = self.client.get(
            reverse("whatsapp-hosted-session-chats", args=[self.account.id])
        )

        self.assertEqual(response.status_code, 200)
        delay.assert_not_called()
        self.assertFalse(response.context["sync_requested"])
