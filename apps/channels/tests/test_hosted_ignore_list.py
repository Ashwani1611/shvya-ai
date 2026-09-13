from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_ignore_service import (
    HostedIgnoreSyncError,
    sync_existing_hosted_chats,
)
from services.channels.hosted_whatsapp_service import handle_gateway_event


class HostedExistingChatIgnoreListTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Ignore List Test Org",
            settings={"hosted_account_enabled": True},
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            country_code="+91",
            phone_number="9999999999",
            is_active=True,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New",
            display_order=0,
            is_active=True,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Sales",
            display_phone_number="+919999999999",
            phone_number_id="+919999999999",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self._set_auto_lead_creation(True)

    def _set_auto_lead_creation(self, enabled):
        settings = dict(self.organization.settings or {})
        settings["hosted_whatsapp"] = {
            "sessions": {
                str(self.account.id): {
                    "auto_lead_creation": enabled,
                    "ai_auto_reply": False,
                }
            }
        }
        self.organization.settings = settings
        self.organization.save(update_fields=["settings", "updated_at"])
        self.account.organization = self.organization

    def _message_payload(self, *, message_id, phone="+919876543210", chat_id=None):
        chat_id = chat_id or f"{phone.lstrip('+')}@c.us"
        return {
            "sessionId": str(self.account.id),
            "event": "message",
            "messageId": message_id,
            "from": f"{phone.lstrip('+')}@c.us",
            "to": "919999999999@c.us",
            "fromMe": False,
            "body": "Hello",
            "messageType": "text",
            "chatId": chat_id,
            "contactName": "Existing Contact",
            "isGroup": False,
        }

    @patch(
        "services.channels.hosted_ignore_service.WhatsAppWebClient.get_existing_chats"
    )
    def test_sync_creates_deduped_snapshot_and_excludes_own_number_and_groups(
        self,
        get_existing_chats,
    ):
        get_existing_chats.return_value = {
            "ok": True,
            "unresolved": 0,
            "chats": [
                {
                    "chatId": "109698229481548@lid",
                    "phoneNumber": "+919876543210",
                    "contactName": "Existing Contact",
                    "isGroup": False,
                },
                {
                    "chatId": "919876543210@c.us",
                    "phoneNumber": "+919876543210",
                    "contactName": "Duplicate",
                    "isGroup": False,
                },
                {
                    "chatId": "919999999999@c.us",
                    "phoneNumber": "+919999999999",
                    "contactName": "Self",
                    "isGroup": False,
                },
                {
                    "chatId": "120363000000@g.us",
                    "phoneNumber": "+911234567890",
                    "contactName": "Group",
                    "isGroup": True,
                },
            ],
        }

        result = sync_existing_hosted_chats(organization=self.organization)

        self.assertEqual(result.account_count, 1)
        self.assertEqual(result.contact_count, 1)
        row = HostedChatIgnoreContact.objects.get()
        self.assertEqual(row.phone_number, "+919876543210")
        self.assertEqual(row.contact_name, "Existing Contact")
        self.assertEqual(row.account, self.account)

    @patch(
        "services.channels.hosted_ignore_service.WhatsAppWebClient.get_existing_chats"
    )
    def test_unresolved_gateway_chat_preserves_previous_snapshot(
        self,
        get_existing_chats,
    ):
        old = HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919111111111",
            contact_name="Old Snapshot",
            chat_id="old@c.us",
        )
        get_existing_chats.return_value = {
            "ok": True,
            "unresolved": 1,
            "chats": [],
        }

        with self.assertRaises(HostedIgnoreSyncError):
            sync_existing_hosted_chats(organization=self.organization)

        self.assertTrue(
            HostedChatIgnoreContact.objects.filter(pk=old.pk).exists()
        )

    def test_ignored_live_chat_auto_creates_lead_after_reengagement(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Existing Contact",
            chat_id="109698229481548@lid",
        )

        message = handle_gateway_event(
            payload=self._message_payload(message_id="ignored-live")
        )

        lead = Lead.objects.get(phone="+919876543210")
        self.assertEqual(message.lead, lead)
        self.assertEqual(lead.pipeline, self.pipeline)
        self.assertEqual(lead.stage, self.stage)
        self.assertTrue(message.raw_payload["ignoredExistingChat"])
        self.assertTrue(message.raw_payload["leadCreationMessage"])
        self.assertEqual(message.status, WhatsAppMessage.Status.RECEIVED)

    def test_lid_message_uses_snapshot_phone_and_auto_creates_lead(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Existing LID Contact",
            chat_id="109698229481548@lid",
        )
        payload = self._message_payload(
            message_id="lid-fallback",
            chat_id="109698229481548@lid",
        )
        payload["from"] = "109698229481548@lid"

        message = handle_gateway_event(payload=payload)

        lead = Lead.objects.get(phone="+919876543210")
        self.assertEqual(message.lead, lead)
        self.assertEqual(message.from_number, "+919876543210")
        self.assertTrue(message.raw_payload["ignoredExistingChat"])
        self.assertTrue(message.raw_payload["leadCreationMessage"])

    def test_ignored_live_chat_is_eligible_for_hosted_ai(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Existing Contact",
            chat_id="919876543210@c.us",
        )
        self.pipeline.ai_enabled = True
        self.pipeline.save(update_fields=["ai_enabled", "updated_at"])

        with (
            patch(
                "apps.ai_engagement.services.ai_permissions.AIPermissionService.evaluate",
                return_value=SimpleNamespace(allowed=True, reason="allowed"),
            ),
            patch(
                "services.channels.hosted_automation_service.enqueue_ai_engagement"
            ) as enqueue_ai,
            self.captureOnCommitCallbacks(execute=True),
        ):
            message = handle_gateway_event(
                payload=self._message_payload(message_id="ignored-live-ai")
            )

        lead = Lead.objects.get(phone="+919876543210")
        self.assertEqual(message.lead, lead)
        self.assertTrue(message.raw_payload["ignoredExistingChat"])
        self.assertTrue(message.raw_payload["leadCreationMessage"])
        self.assertTrue(enqueue_ai.called)
        self.assertTrue(
            any(
                call.kwargs.get("account") == self.account
                and call.kwargs.get("lead") == lead
                and call.kwargs.get("source_message") == message
                for call in enqueue_ai.call_args_list
            )
        )

    def test_manually_created_lead_attaches_even_when_number_is_ignored(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Existing Contact",
            chat_id="919876543210@c.us",
        )
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Manual Lead",
            phone="+919876543210",
        )

        message = handle_gateway_event(
            payload=self._message_payload(message_id="manual-lead")
        )

        self.assertEqual(message.lead, lead)
        self.assertTrue(message.raw_payload["ignoredExistingChat"])
        self.assertEqual(Lead.objects.count(), 1)

    def test_resetting_ignore_rows_allows_future_auto_lead_creation(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Existing Contact",
            chat_id="919876543210@c.us",
        )
        HostedChatIgnoreContact.objects.filter(
            organization=self.organization
        ).delete()

        message = handle_gateway_event(
            payload=self._message_payload(message_id="after-reset")
        )

        lead = Lead.objects.get(phone="+919876543210")
        self.assertEqual(message.lead, lead)
        self.assertFalse(message.raw_payload["ignoredExistingChat"])
