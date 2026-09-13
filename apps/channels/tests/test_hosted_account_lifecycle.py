from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline, Stage
from apps.followups.models import FollowupSequence
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import create_hosted_account


class HostedAccountLifecycleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Lifecycle Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-lifecycle@example.com",
            password="test-password",
            name="Hosted Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Hosted Lifecycle Sales",
            country_code="+91",
            phone_number="8700274739",
            owner=self.user,
        )
        Stage.objects.get_or_create(
            pipeline=self.pipeline,
            display_order=1,
            defaults={"name": "New"},
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def create_account(self):
        account, _pipeline, _created = create_hosted_account(
            organization=self.org,
            created_by=self.user,
            country_code="+91",
            phone_number="8700274739",
        )
        return account

    def create_message(self, account, external_id="wweb:lifecycle-1"):
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=account,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=external_id,
            from_number="+919876543210",
            to_number=account.display_phone_number,
            body="Hello",
            status=WhatsAppMessage.Status.RECEIVED,
        )

    def test_disconnected_hosted_account_clears_its_chat_history(self):
        account = self.create_account()
        message = self.create_message(account)

        account.status = WhatsAppAccount.Status.DISCONNECTED
        account.save(update_fields=["status", "updated_at"])

        self.assertFalse(WhatsAppMessage.objects.filter(id=message.id).exists())
        self.assertTrue(WhatsAppAccount.objects.filter(id=account.id).exists())

    @patch("apps.channels.hosted_manage_ui.initialize_hosted_session_task.delay")
    @patch("apps.channels.hosted_manage_ui.hosted_chat_ui._repair_live_status")
    def test_disconnected_chat_click_redirects_to_qr_login(
        self,
        repair_live_status,
        initialize_delay,
    ):
        account = self.create_account()
        account.status = WhatsAppAccount.Status.DISCONNECTED
        account.save(update_fields=["status", "updated_at"])

        response = self.client.get(
            reverse("whatsapp-hosted-session-chats", args=[account.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f"{reverse('whatsapp-connect-hosted')}?login={account.id}",
        )
        repair_live_status.assert_called_once()
        initialize_delay.assert_called_once_with(str(account.id))
        account.refresh_from_db()
        self.assertEqual(account.status, WhatsAppAccount.Status.PENDING)

    def test_hosted_account_page_exposes_admin_delete_action(self):
        account = self.create_account()

        response = self.client.get(reverse("whatsapp-connect-hosted"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete Hosted Account")
        self.assertContains(
            response,
            reverse("whatsapp-hosted-session-delete", args=[account.id]),
        )

    @patch("apps.channels.providers.whatsapp_web.WhatsAppWebClient.logout")
    def test_delete_hosted_account_removes_account_messages_settings_and_sequence(
        self,
        gateway_logout,
    ):
        gateway_logout.return_value = {"status": "disconnected"}
        account = self.create_account()
        message = self.create_message(account, external_id="wweb:delete-lifecycle")
        sequence = FollowupSequence.objects.create(
            organization=self.org,
            name="Hosted sender sequence",
            whatsapp_account=account,
            created_by=self.user,
        )

        response = self.client.post(
            reverse("whatsapp-hosted-session-delete", args=[account.id]),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(WhatsAppAccount.objects.filter(id=account.id).exists())
        self.assertFalse(WhatsAppMessage.objects.filter(id=message.id).exists())
        self.assertFalse(FollowupSequence.objects.filter(id=sequence.id).exists())
        self.org.refresh_from_db()
        sessions = (
            (self.org.settings or {})
            .get("hosted_whatsapp", {})
            .get("sessions", {})
        )
        self.assertNotIn(str(account.id), sessions)
        gateway_logout.assert_called_once_with(session_id=account.id)

    def test_disconnect_cleanup_does_not_touch_official_api_messages(self):
        account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Official API",
            phone_number_id="123456",
            display_phone_number="+919999999999",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        message = self.create_message(account, external_id="wamid:official-api")

        account.status = WhatsAppAccount.Status.DISCONNECTED
        account.save(update_fields=["status", "updated_at"])

        self.assertTrue(WhatsAppMessage.objects.filter(id=message.id).exists())
