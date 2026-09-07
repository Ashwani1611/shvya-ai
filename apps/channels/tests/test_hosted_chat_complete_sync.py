from django.test import TestCase

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
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


class HostedChatCompleteSyncTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Complete Sync Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-complete@example.com",
            password="test-password",
            name="Hosted Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Hosted Sales",
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

    def _resolved_item(
        self,
        *,
        message_id,
        phone,
        raw_chat_id,
        body,
        name="",
        timestamp=1_725_000_000,
        from_me=False,
    ):
        account_digits = self.account.display_phone_number.lstrip("+")
        phone_digits = phone.lstrip("+")
        return {
            "messageId": message_id,
            "from": (
                f"{account_digits}@c.us" if from_me else f"{phone_digits}@c.us"
            ),
            "to": (
                f"{phone_digits}@c.us" if from_me else f"{account_digits}@c.us"
            ),
            "fromMe": from_me,
            "body": body,
            "messageType": "text",
            "timestamp": timestamp,
            "status": "read" if from_me else "",
            "chatId": raw_chat_id,
            "rawChatId": raw_chat_id,
            "peerKey": phone,
            "peerPhone": phone,
            "chatName": name,
            "contactName": name,
            "isGroup": False,
        }

    def test_resolved_lid_alias_merges_old_and_live_rows_into_one_thread(self):
        raw_chat_id = "123456789012@lid"
        real_phone = "+919876543210"

        # Simulate a message imported before WhatsApp exposed the LID -> phone
        # mapping. Legacy persistence therefore stored the LID digits as if
        # they were the peer phone.
        handle_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [
                    {
                        "messageId": "OLD-LID-1",
                        "from": raw_chat_id,
                        "to": "919319988591@c.us",
                        "fromMe": False,
                        "body": "Older unresolved message",
                        "messageType": "text",
                        "timestamp": 1_725_000_000,
                        "chatId": raw_chat_id,
                        "rawChatId": raw_chat_id,
                        "chatName": "",
                        "isGroup": False,
                    }
                ],
            }
        )

        # A later live/history payload resolves the same raw LID to the actual
        # phone number. The read-model must immediately canonicalize both rows
        # without waiting for a database rewrite of the older message.
        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "message",
                **self._resolved_item(
                    message_id="LIVE-RESOLVED-1",
                    phone=real_phone,
                    raw_chat_id=raw_chat_id,
                    body="New realtime message",
                    name="Neha Sharma",
                    timestamp=1_725_000_100,
                ),
            }
        )

        snapshot = build_hosted_chat_snapshot(
            account=self.account,
            selected_chat=real_phone,
        )

        self.assertEqual(snapshot["total_conversations"], 1)
        self.assertEqual(snapshot["selected_chat"], real_phone)
        self.assertEqual(snapshot["selected_name"], "Neha Sharma")
        self.assertEqual(
            [message.body for message in snapshot["thread"]],
            ["Older unresolved message", "New realtime message"],
        )

    def test_search_uses_best_name_even_when_latest_message_has_only_phone(self):
        raw_chat_id = "223456789012@lid"
        real_phone = "+919811112222"

        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [
                    self._resolved_item(
                        message_id="NAMED-OLDER-1",
                        phone=real_phone,
                        raw_chat_id=raw_chat_id,
                        body="Older named message",
                        name="Neha Sharma",
                        timestamp=1_725_000_000,
                    ),
                    self._resolved_item(
                        message_id="UNNAMED-LATEST-1",
                        phone=real_phone,
                        raw_chat_id=raw_chat_id,
                        body="Latest message",
                        name="",
                        timestamp=1_725_000_100,
                    ),
                ],
            }
        )

        by_name = build_hosted_chat_snapshot(account=self.account, query="neha")
        self.assertEqual(len(by_name["conversations"]), 1)
        self.assertEqual(by_name["conversations"][0]["name"], "Neha Sharma")

        by_number = build_hosted_chat_snapshot(account=self.account, query="9811112222")
        self.assertEqual(len(by_number["conversations"]), 1)
        self.assertEqual(by_number["conversations"][0]["phone"], real_phone)

    def test_selected_lid_url_is_normalized_to_resolved_phone(self):
        raw_chat_id = "323456789012@lid"
        real_phone = "+919833334444"
        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [
                    self._resolved_item(
                        message_id="SELECT-ALIAS-1",
                        phone=real_phone,
                        raw_chat_id=raw_chat_id,
                        body="Resolved conversation",
                        name="Rohan Verma",
                    )
                ],
            }
        )

        snapshot = build_hosted_chat_snapshot(
            account=self.account,
            selected_chat=raw_chat_id,
        )
        self.assertEqual(snapshot["selected_chat"], real_phone)
        self.assertEqual(snapshot["selected_name"], "Rohan Verma")
        self.assertEqual([m.body for m in snapshot["thread"]], ["Resolved conversation"])
