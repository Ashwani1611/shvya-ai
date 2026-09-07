from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_ignore_service import (
    reset_hosted_ignore_list,
    sync_existing_hosted_chats,
)
from services.channels.hosted_whatsapp_service import (
    create_hosted_account,
    handle_gateway_event,
    update_session_settings,
)


class HostedIgnoreListTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted Ignore Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-ignore@example.com",
            password="test-password",
            name="Hosted Ignore Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Hosted Ignore Sales",
            country_code="+91",
            phone_number="8700274739",
            owner=self.user,
        )
        Stage.objects.get_or_create(
            pipeline=self.pipeline,
            display_order=1,
            defaults={"name": "New"},
        )
        self.account, _pipeline, _created = create_hosted_account(
            organization=self.organization,
            created_by=self.user,
            country_code="+91",
            phone_number="8700274739",
        )
        self.account.status = WhatsAppAccount.Status.CONNECTED
        self.account.save(update_fields=["status", "updated_at"])
        update_session_settings(
            account=self.account,
            payload={
                "auto_lead_creation": True,
                "ai_auto_reply": False,
            },
        )
        self.account.refresh_from_db()
        self.account.organization.refresh_from_db()

    @patch(
        "services.channels.hosted_ignore_service.WhatsAppWebClient.get_existing_chats"
    )
    def test_sync_builds_replaceable_snapshot_from_direct_chats(self, get_existing_chats):
        get_existing_chats.return_value = {
            "ok": True,
            "chats": [
                {
                    "chatId": "919811112222@c.us",
                    "phoneNumber": "+919811112222",
                    "contactName": "Old Customer",
                    "isGroup": False,
                },
                {
                    "chatId": "919811112222@c.us",
                    "phoneNumber": "+919811112222",
                    "contactName": "Updated Customer",
                    "isGroup": False,
                },
                {
                    "chatId": "120363000000@g.us",
                    "phoneNumber": "+919822223333",
                    "contactName": "A Group",
                    "isGroup": True,
                },
                {
                    "chatId": "self@c.us",
                    "phoneNumber": "+918700274739",
                    "contactName": "Self",
                    "isGroup": False,
                },
            ],
        }

        result = sync_existing_hosted_chats(organization=self.organization)

        self.assertEqual(result.account_count, 1)
        self.assertEqual(result.contact_count, 1)
        contact = HostedChatIgnoreContact.objects.get(
            organization=self.organization,
            account=self.account,
        )
        self.assertEqual(contact.phone_number, "+919811112222")
        self.assertEqual(contact.contact_name, "Updated Customer")

        get_existing_chats.return_value = {
            "ok": True,
            "chats": [
                {
                    "chatId": "919833334444@c.us",
                    "phoneNumber": "+919833334444",
                    "contactName": "Fresh Snapshot Contact",
                    "isGroup": False,
                }
            ],
        }
        second = sync_existing_hosted_chats(organization=self.organization)

        self.assertEqual(second.contact_count, 1)
        self.assertFalse(
            HostedChatIgnoreContact.objects.filter(
                account=self.account,
                phone_number="+919811112222",
            ).exists()
        )
        self.assertTrue(
            HostedChatIgnoreContact.objects.filter(
                account=self.account,
                phone_number="+919833334444",
            ).exists()
        )

    def test_ignored_existing_chat_does_not_auto_create_lead(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Existing Customer",
            chat_id="919876543210@c.us",
        )

        message = handle_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "message",
                "messageId": "IGNORED-LIVE-1",
                "from": "919876543210@c.us",
                "to": "918700274739@c.us",
                "chatId": "919876543210@c.us",
                "contactName": "Existing Customer",
                "body": "Hello again",
                "messageType": "text",
                "isGroup": False,
            }
        )

        self.assertFalse(
            Lead.objects.filter(
                organization=self.organization,
                phone="+919876543210",
            ).exists()
        )
        self.assertIsNone(message.lead)
        self.assertEqual(message.status, WhatsAppMessage.Status.RECEIVED)

    def test_reset_allows_future_live_message_to_auto_create_lead(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Existing Customer",
            chat_id="919876543210@c.us",
        )

        reset_hosted_ignore_list(organization=self.organization)

        message = handle_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "message",
                "messageId": "AFTER-RESET-LIVE-1",
                "from": "919876543210@c.us",
                "to": "918700274739@c.us",
                "chatId": "919876543210@c.us",
                "contactName": "Existing Customer",
                "body": "Hello after reset",
                "messageType": "text",
                "isGroup": False,
            }
        )

        lead = Lead.objects.get(
            organization=self.organization,
            phone="+919876543210",
        )
        self.assertEqual(message.lead, lead)
        self.assertEqual(lead.pipeline, self.pipeline)
