from django.test import TestCase

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.hosted_contact_sync import sync_hosted_contact_names


class HostedContactNameSyncTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Hosted Contact Name Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-contact-name@example.com",
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
        self.stage, _created = Stage.objects.get_or_create(
            pipeline=self.pipeline,
            display_order=1,
            defaults={"name": "New"},
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="hosted",
            status=WhatsAppAccount.Status.CONNECTED,
            display_phone_number="+919319988591",
            phone_number_id="+919319988591",
            is_active=True,
        )

    def payload(self, *, phone="+919765011746", name="Govind", event="message"):
        item = {
            "messageId": "CONTACT-NAME-1",
            "from": f"{phone.lstrip('+')}@c.us",
            "to": "919319988591@c.us",
            "fromMe": False,
            "body": "Hello",
            "messageType": "text",
            "peerPhone": phone,
            "contactPhoneNumber": phone,
            "contactName": name,
            "chatName": name,
            "isGroup": False,
        }
        if event == "history_sync":
            return {
                "sessionId": str(self.account.id),
                "event": "history_sync",
                "messages": [item],
            }
        return {
            "sessionId": str(self.account.id),
            "event": "message",
            **item,
        }

    def create_lead(self, *, phone, name, lead_source="system"):
        return Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            phone=phone,
            name=name,
            lead_source=lead_source,
        )

    def test_live_whatsapp_name_replaces_phone_placeholder(self):
        lead = self.create_lead(
            phone="+919765011746",
            name="+919765011746",
        )

        changed = sync_hosted_contact_names(payload=self.payload())

        lead.refresh_from_db()
        self.assertEqual(changed, 1)
        self.assertEqual(lead.name, "Govind")

    def test_history_sync_updates_whatsapp_created_lead_name(self):
        lead = self.create_lead(
            phone="+919765011746",
            name="Old WhatsApp Name",
            lead_source="whatsapp_api",
        )

        changed = sync_hosted_contact_names(
            payload=self.payload(name="Govind", event="history_sync")
        )

        lead.refresh_from_db()
        self.assertEqual(changed, 1)
        self.assertEqual(lead.name, "Govind")

    def test_manual_lead_name_is_not_overwritten(self):
        lead = self.create_lead(
            phone="+919765011746",
            name="India Rugs",
            lead_source="system",
        )

        changed = sync_hosted_contact_names(payload=self.payload(name="Govind"))

        lead.refresh_from_db()
        self.assertEqual(changed, 0)
        self.assertEqual(lead.name, "India Rugs")

    def test_group_name_is_never_copied_to_a_lead(self):
        lead = self.create_lead(
            phone="+919765011746",
            name="+919765011746",
        )
        payload = self.payload(name="Project Group")
        payload.update(
            {
                "isGroup": True,
                "peerPhone": "",
                "contactPhoneNumber": "",
                "from": "120363012345678901@g.us",
            }
        )

        changed = sync_hosted_contact_names(payload=payload)

        lead.refresh_from_db()
        self.assertEqual(changed, 0)
        self.assertEqual(lead.name, "+919765011746")
