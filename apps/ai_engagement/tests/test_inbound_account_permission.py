from django.test import TestCase

from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class InboundAccountPermissionRegressionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Hosted Permission Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Hosted Sales",
            country_code="+91",
            phone_number="9876543210",
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Hosted Lead",
            phone="+919111111111",
        )
        self.hosted_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Sales",
            phone_number_id="+919876543210",
            display_phone_number="+919876543210",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def test_newer_outbound_on_another_number_does_not_block_inbound_ai(self):
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.hosted_account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="hosted-customer-turn",
            from_number=self.lead.phone,
            to_number=self.hosted_account.display_phone_number,
            body="Hello",
            status=WhatsAppMessage.Status.RECEIVED,
        )

        other_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Other Number",
            phone_number_id="meta-other-number",
            display_phone_number="+918888888888",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=other_account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            external_id="newer-agent-outbound",
            from_number=other_account.display_phone_number,
            to_number=self.lead.phone,
            body="Manual message from another connected number",
            status=WhatsAppMessage.Status.SENT,
        )

        decision = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_wrong_inbound_number_still_fails_closed(self):
        wrong_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Wrong Inbound Number",
            phone_number_id="meta-wrong-number",
            display_phone_number="+918888888888",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=wrong_account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wrong-customer-turn",
            from_number=self.lead.phone,
            to_number=wrong_account.display_phone_number,
            body="Hello on the wrong number",
            status=WhatsAppMessage.Status.RECEIVED,
        )

        decision = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "pipeline_whatsapp_account_mismatch")
