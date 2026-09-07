from django.test import TestCase

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_chat_service import (
    build_hosted_chat_snapshot,
    handle_hosted_gateway_event,
)
from services.channels.hosted_whatsapp_service import (
    create_hosted_account,
    handle_gateway_event,
)


class HostedLidAliasRepairTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted LID Alias Repair Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-lid-alias@example.com",
            password="test-password",
            name="Hosted Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Hosted LID Sales",
            country_code="+91",
            phone_number="9319988591",
            owner=self.user,
        )
        Stage.objects.get_or_create(
            pipeline=self.pipeline,
            display_order=1,
            defaults={"name": "New"},
        )
        self.account, _pipeline, _created = create_hosted_account(
            organization=self.org,
            created_by=self.user,
            country_code="+91",
            phone_number="9319988591",
        )
        self.account.status = WhatsAppAccount.Status.CONNECTED
        self.account.save(update_fields=["status", "updated_at"])

    def test_older_lid_derived_phone_cannot_override_newer_real_phone(self):
        raw_chat_id = "59906237419749@lid"
        fake_lid_phone = "+59906237419749"
        real_phone = "+919877916050"
        account_digits = self.account.display_phone_number.lstrip("+")

        handle_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [
                    {
                        "messageId": "LEGACY-LID-PSEUDO-PHONE",
                        "from": raw_chat_id,
                        "to": f"{account_digits}@c.us",
                        "fromMe": False,
                        "body": "Old message",
                        "messageType": "text",
                        "timestamp": 1_725_000_000,
                        "chatId": raw_chat_id,
                        "rawChatId": raw_chat_id,
                        "chatName": "Sisupal Bsf",
                        "isGroup": False,
                    }
                ],
            }
        )

        old = WhatsAppMessage.objects.get(
            external_id="wweb:LEGACY-LID-PSEUDO-PHONE"
        )
        old.from_number = fake_lid_phone
        old.raw_payload = {
            **old.raw_payload,
            "rawChatId": raw_chat_id,
            "chatId": raw_chat_id,
            "peerKey": fake_lid_phone,
            "peerPhone": fake_lid_phone,
            "contactName": "Sisupal Bsf",
            "timestamp": 1_725_000_000,
        }
        old.save(update_fields=["from_number", "raw_payload", "updated_at"])

        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "message",
                "messageId": "RESOLVED-LID-REAL-PHONE",
                "from": "919877916050@c.us",
                "to": f"{account_digits}@c.us",
                "fromMe": False,
                "body": "New message",
                "messageType": "text",
                "timestamp": 1_725_000_100,
                "status": "",
                "chatId": raw_chat_id,
                "rawChatId": raw_chat_id,
                "peerKey": real_phone,
                "peerPhone": real_phone,
                "chatName": "Sisupal Bsf",
                "contactName": "Sisupal Bsf",
                "isGroup": False,
            }
        )

        snapshot = build_hosted_chat_snapshot(
            account=self.account,
            selected_chat=real_phone,
        )

        self.assertEqual(snapshot["total_conversations"], 1)
        self.assertEqual(snapshot["selected_chat"], real_phone)
        self.assertEqual(snapshot["conversations"][0]["phone"], real_phone)
        self.assertEqual(
            [message.body for message in snapshot["thread"]],
            ["Old message", "New message"],
        )
