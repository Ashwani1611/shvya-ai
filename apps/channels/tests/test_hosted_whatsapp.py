from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    create_hosted_account,
    handle_gateway_event,
    normalize_whatsapp_number,
    update_session_settings,
)


class HostedWhatsAppTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Hosted Org")
        self.user = User.objects.create_user(
            email="hosted@example.com",
            password="test-password",
            name="Hosted Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Hosted Sales",
            country_code="+91",
            phone_number="8700274739",
            owner=self.user,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New",
            display_order=1,
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

    def test_number_normalization_does_not_duplicate_country_code(self):
        self.assertEqual(
            normalize_whatsapp_number(
                country_code="+91",
                phone_number="+91 87002 74739",
            ),
            "+918700274739",
        )
        self.assertEqual(
            normalize_whatsapp_number(
                country_code="+91",
                phone_number="918700274739",
            ),
            "+918700274739",
        )

    def test_session_number_must_match_active_pipeline(self):
        with self.assertRaises(HostedWhatsAppValidationError):
            create_hosted_account(
                organization=self.org,
                created_by=self.user,
                country_code="+91",
                phone_number="9999999999",
            )

    def test_create_hosted_account_reuses_whatsapp_account_model(self):
        account = self.create_account()
        self.assertEqual(account.connection_type, "hosted")
        self.assertEqual(account.display_phone_number, "+918700274739")
        self.assertEqual(account.status, WhatsAppAccount.Status.PENDING)
        self.org.refresh_from_db()
        session_settings = self.org.settings["hosted_whatsapp"]["sessions"][str(account.id)]
        self.assertFalse(session_settings["ai_auto_reply"])
        self.assertTrue(session_settings["auto_follow_up"])
        self.assertEqual(session_settings["bump_up_count"], 2)

    @patch("apps.channels.hosted_ui.initialize_hosted_session_task.delay")
    def test_create_endpoint_queues_gateway_initialization(self, delay):
        response = self.client.post(
            reverse("whatsapp-hosted-session-create"),
            data={
                "country_code": "+91",
                "phone_number": "8700274739",
            },
        )
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertTrue(payload["ok"])
        account = WhatsAppAccount.objects.get(id=payload["account_id"])
        delay.assert_called_once_with(str(account.id))

    @patch("apps.channels.hosted_ui.initialize_hosted_session_task.delay")
    def test_create_endpoint_rejects_number_not_mapped_to_pipeline(self, delay):
        response = self.client.post(
            reverse("whatsapp-hosted-session-create"),
            data={
                "country_code": "+91",
                "phone_number": "9999999999",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not linked", response.json()["error"])
        delay.assert_not_called()

    def test_hosted_page_has_exact_five_actions_in_required_order(self):
        account = self.create_account()
        response = self.client.get(reverse("whatsapp-connect-hosted"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        labels = [
            "Get QR Code",
            "View Chats",
            "View Queue",
            "Session Settings",
            "Logout Session",
        ]
        positions = [html.index(f'title="{label}"') for label in labels]
        self.assertEqual(positions, sorted(positions))
        for label in labels:
            self.assertEqual(html.count(f'title="{label}"'), 1)
        self.assertContains(response, str(account.display_phone_number))

    def test_gateway_message_can_create_pipeline_lead_when_enabled(self):
        account = self.create_account()
        update_session_settings(
            account=account,
            payload={
                "auto_lead_creation": True,
                "ai_auto_reply": False,
            },
        )
        account.refresh_from_db()
        account.organization.refresh_from_db()

        message = handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": "message",
                "messageId": "ABC123",
                "from": "919876543210@c.us",
                "to": "918700274739@c.us",
                "chatId": "919876543210@c.us",
                "contactName": "Hosted Lead",
                "body": "Hello",
                "messageType": "text",
                "isGroup": False,
            }
        )
        lead = Lead.objects.get(organization=self.org, phone="+919876543210")
        self.assertEqual(lead.pipeline, self.pipeline)
        self.assertEqual(lead.lead_source, "whatsapp_api")
        self.assertEqual(message.lead, lead)
        self.assertEqual(message.external_id, "wweb:ABC123")

        duplicate = handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": "message",
                "messageId": "ABC123",
                "from": "919876543210@c.us",
                "body": "Hello again",
            }
        )
        self.assertEqual(duplicate.id, message.id)
        self.assertEqual(
            WhatsAppMessage.objects.filter(external_id="wweb:ABC123").count(),
            1,
        )

    def test_ready_event_rejects_scanned_wrong_number(self):
        account = self.create_account()
        result = handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": "ready",
                "phoneNumber": "+919999999999",
            }
        )
        self.assertEqual(result.id, account.id)
        account.refresh_from_db()
        self.assertEqual(account.status, WhatsAppAccount.Status.FAILED)
