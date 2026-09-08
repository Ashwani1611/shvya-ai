from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_api_chat_service import list_api_conversations


class WhatsAppApiChatScopeAndSourceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="WhatsApp Scope Org")
        self.user = User.objects.create_user(
            email="scope@example.com",
            password="test-password",
            name="Scope Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Sales",
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New",
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def _lead(self, phone, source="system"):
        return Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name=f"Lead {phone[-4:]}",
            phone=phone,
            lead_source=source,
        )

    def _message(self, account, lead, body="Hello"):
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=account,
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            body=body,
            from_number=lead.phone,
            to_number=account.display_phone_number or "+919999999999",
        )

    def test_disconnected_api_account_conversations_are_hidden(self):
        connected = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="connected-api",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        disconnected = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="old-api",
            status=WhatsAppAccount.Status.DISCONNECTED,
            is_active=True,
        )
        connected_lead = self._lead("+919000000101", "whatsapp_api")
        old_lead = self._lead("+919000000102", "whatsapp_api")
        self._message(connected, connected_lead, "Current")
        self._message(disconnected, old_lead, "Old")

        conversations = list_api_conversations(organization=self.org)
        self.assertEqual(list(conversations.values_list("id", flat=True)), [connected_lead.id])

        response = self.client.get(reverse("whatsapp-chats"))
        self.assertContains(response, connected_lead.name)
        self.assertNotContains(response, old_lead.name)

    def test_connected_numbers_lists_only_connected_api_accounts(self):
        visible = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="visible-api",
            display_phone_number="+919000000201",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="disconnected-api",
            display_phone_number="+919000000202",
            status=WhatsAppAccount.Status.DISCONNECTED,
            is_active=True,
        )
        WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            phone_number_id="hosted",
            display_phone_number="+919000000203",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

        response = self.client.get(reverse("whatsapp-accounts"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["accounts"]), [visible])

    def test_first_hosted_inbound_normalizes_new_lead_source(self):
        hosted = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            phone_number_id="hosted-source",
            display_phone_number="+919000000301",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        lead = self._lead("+919000000302", "whatsapp_api")
        self._message(hosted, lead)

        lead.refresh_from_db()
        self.assertEqual(lead.lead_source, "whatsapp")

    def test_existing_api_lead_is_not_relabelled_by_later_hosted_message(self):
        hosted = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            phone_number_id="hosted-existing",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        api = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="api-existing",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        lead = self._lead("+919000000401", "whatsapp_api")
        self._message(api, lead, "API first")
        self._message(hosted, lead, "Hosted later")

        lead.refresh_from_db()
        self.assertEqual(lead.lead_source, "whatsapp_api")
