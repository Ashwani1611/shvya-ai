import json
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
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


class HostedChatRealtimeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Realtime Hosted Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-realtime@example.com",
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

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def history_item(
        self,
        *,
        message_id,
        phone,
        raw_chat_id,
        name,
        body,
        from_me=False,
        timestamp=1_725_000_000,
    ):
        phone_digits = phone.lstrip("+")
        account_digits = self.account.display_phone_number.lstrip("+")
        return {
            "messageId": message_id,
            "from": (
                f"{account_digits}@c.us" if from_me else raw_chat_id
            ),
            "to": (
                raw_chat_id if from_me else f"{account_digits}@c.us"
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
            "phoneDigitsForTest": phone_digits,
        }

    def test_lid_history_is_grouped_by_real_phone_and_name(self):
        item = self.history_item(
            message_id="LID-HISTORY-1",
            phone="+919812345678",
            raw_chat_id="123456789012@lid",
            name="Aarav Customer",
            body="Hello from LID chat",
        )

        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [item],
            }
        )

        message = WhatsAppMessage.objects.get(external_id="wweb:LID-HISTORY-1")
        self.assertEqual(message.from_number, "+919812345678")
        self.assertEqual(message.to_number, "+919319988591")
        self.assertEqual(message.raw_payload["peerPhone"], "+919812345678")
        self.assertEqual(message.raw_payload["rawChatId"], "123456789012@lid")

        snapshot = build_hosted_chat_snapshot(account=self.account)
        self.assertEqual(len(snapshot["conversations"]), 1)
        row = snapshot["conversations"][0]
        self.assertEqual(row["key"], "+919812345678")
        self.assertEqual(row["phone"], "+919812345678")
        self.assertEqual(row["name"], "Aarav Customer")
        self.assertEqual([m.body for m in snapshot["thread"]], ["Hello from LID chat"])

    def test_resync_repairs_existing_message_imported_with_lid_as_phone(self):
        old_payload = {
            "sessionId": str(self.account.id),
            "event": "history_sync",
            "messages": [
                {
                    "messageId": "LID-REPAIR-1",
                    "from": "123456789012@lid",
                    "to": "919319988591@c.us",
                    "fromMe": False,
                    "body": "Imported before identity fix",
                    "messageType": "text",
                    "timestamp": 1_725_000_000,
                    "chatId": "123456789012@lid",
                    "chatName": "Customer",
                    "isGroup": False,
                }
            ],
        }
        handle_gateway_event(payload=old_payload)
        message = WhatsAppMessage.objects.get(external_id="wweb:LID-REPAIR-1")

        # Simulate the legacy persisted state that existed before LIDs were
        # explicitly rejected as phone numbers. Current ingestion correctly
        # stores unresolved LIDs as unknown until a real peer phone arrives.
        message.from_number = "+123456789012"
        message.save(update_fields=["from_number"])

        repaired = self.history_item(
            message_id="LID-REPAIR-1",
            phone="+919876543210",
            raw_chat_id="123456789012@lid",
            name="Real Customer Name",
            body="Imported before identity fix",
        )
        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [repaired],
            }
        )

        message.refresh_from_db()
        self.assertEqual(message.from_number, "+919876543210")
        self.assertEqual(message.raw_payload["peerPhone"], "+919876543210")
        self.assertEqual(
            WhatsAppMessage.objects.filter(external_id="wweb:LID-REPAIR-1").count(),
            1,
        )

    def test_search_matches_contact_name_and_phone_digits(self):
        items = [
            self.history_item(
                message_id="SEARCH-ONE",
                phone="+919811112222",
                raw_chat_id="111111111111@lid",
                name="Neha Sharma",
                body="First chat",
            ),
            self.history_item(
                message_id="SEARCH-TWO",
                phone="+919833334444",
                raw_chat_id="222222222222@lid",
                name="Rohan Verma",
                body="Second chat",
                timestamp=1_725_000_100,
            ),
        ]
        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": items,
            }
        )

        by_name = build_hosted_chat_snapshot(
            account=self.account,
            query="neha",
        )
        self.assertEqual([r["name"] for r in by_name["conversations"]], ["Neha Sharma"])

        by_number = build_hosted_chat_snapshot(
            account=self.account,
            query="9811112222",
        )
        self.assertEqual(len(by_number["conversations"]), 1)
        self.assertEqual(by_number["conversations"][0]["phone"], "+919811112222")

    def test_selected_thread_contains_both_directions_in_time_order(self):
        phone = "+919855556666"
        items = [
            self.history_item(
                message_id="THREAD-IN-1",
                phone=phone,
                raw_chat_id="333333333333@lid",
                name="Thread Contact",
                body="Inbound one",
                timestamp=1_725_000_000,
            ),
            self.history_item(
                message_id="THREAD-OUT-1",
                phone=phone,
                raw_chat_id="333333333333@lid",
                name="Thread Contact",
                body="Outbound one",
                from_me=True,
                timestamp=1_725_000_010,
            ),
            self.history_item(
                message_id="THREAD-IN-2",
                phone=phone,
                raw_chat_id="333333333333@lid",
                name="Thread Contact",
                body="Inbound two",
                timestamp=1_725_000_020,
            ),
        ]
        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": items,
            }
        )

        snapshot = build_hosted_chat_snapshot(
            account=self.account,
            selected_chat=phone,
        )
        self.assertEqual(
            [m.body for m in snapshot["thread"]],
            ["Inbound one", "Outbound one", "Inbound two"],
        )
        self.assertEqual(
            [m.direction for m in snapshot["thread"]],
            ["inbound", "outbound", "inbound"],
        )

    @patch("apps.channels.hosted_chat_ui._request_history_refresh", return_value=False)
    def test_data_endpoint_performs_server_side_search(self, _refresh):
        handle_hosted_gateway_event(
            payload={
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [
                    self.history_item(
                        message_id="ENDPOINT-SEARCH",
                        phone="+919899998888",
                        raw_chat_id="444444444444@lid",
                        name="Searchable Person",
                        body="Hello",
                    )
                ],
            }
        )
        response = self.client.get(
            reverse(
                "whatsapp-hosted-session-chats-data",
                args=[self.account.id],
            ),
            {"q": "99998888"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["conversations"]), 1)
        self.assertEqual(payload["conversations"][0]["name"], "Searchable Person")

    @patch("apps.channels.hosted_chat_ui.send_whatsapp_message_task.delay")
    def test_lid_chat_can_be_queued_without_treating_lid_as_phone(self, delay):
        response = self.client.post(
            reverse(
                "whatsapp-hosted-session-chat-send",
                args=[self.account.id],
            ),
            data=json.dumps(
                {
                    "chat": "555555555555@lid",
                    "body": "Reply to unresolved LID chat",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        message = WhatsAppMessage.objects.get(id=response.json()["message"]["id"])
        self.assertEqual(message.to_number, "555555555555@lid")
        self.assertEqual(message.raw_payload["peerKey"], "555555555555@lid")
        delay.assert_called_once_with(str(message.id))

    @patch("apps.channels.hosted_chat_ui._request_history_refresh", return_value=False)
    @patch("apps.channels.hosted_chat_ui._repair_live_status")
    def test_hosted_chat_page_contains_realtime_socket_and_data_api(
        self,
        _repair,
        _refresh,
    ):
        response = self.client.get(
            reverse("whatsapp-hosted-session-chats", args=[self.account.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "new WebSocket")
        self.assertContains(
            response,
            reverse("whatsapp-hosted-session-chats-data", args=[self.account.id]),
        )
        self.assertContains(response, "Search by name or number")
