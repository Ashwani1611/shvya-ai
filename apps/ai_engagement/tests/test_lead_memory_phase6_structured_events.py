from django.test import TestCase

from apps.ai_engagement.models import LeadMemory
from apps.ai_engagement.services.lead_memory import LeadMemoryService
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class LeadMemoryStructuredEventFactTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Memory Categories Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.pipeline.stages.get(name="New leads"),
            name="Lead",
            phone="+919100000001",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type="api",
            display_phone_number="+919876543210",
            phone_number_id="memory-categories-phone",
            status="connected",
            is_active=True,
        )

    def test_objection_and_commitment_are_structured_facts_and_long_term_events(self):
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="wamid-memory-categories",
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body=(
                "The price is high, I need to think. "
                "I will send the documents tomorrow."
            ),
            status="received",
        )

        LeadMemoryService().update_from_accepted_turn(
            organization=self.organization,
            lead=self.lead,
            source_message_id=message.id,
        )

        memory = LeadMemory.objects.get(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertEqual(
            memory.structured_facts["concept:objection"]["source"],
            "explicit_message",
        )
        self.assertEqual(
            memory.structured_facts["concept:commitment"]["source"],
            "explicit_message",
        )
        self.assertEqual(
            memory.structured_facts["concept:objection"]["evidence"]["message_id"],
            str(message.id),
        )
        self.assertEqual(
            memory.structured_facts["concept:commitment"]["evidence"]["message_id"],
            str(message.id),
        )
        event_types = {event["type"] for event in memory.long_term_events}
        self.assertIn("objection", event_types)
        self.assertIn("commitment", event_types)
