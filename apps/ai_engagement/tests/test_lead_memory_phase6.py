import json
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.models import AITrace, LeadMemory, OrgInfo
from apps.ai_engagement.services.lead_memory import (
    LeadMemoryScopeError,
    LeadMemoryService,
)
from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_state import QUALIFICATION_STATE_KEY
from apps.ai_engagement.services.trace_service import (
    begin_trace,
    finalize_from_result,
    flush,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.organizations.models import Organization


class LeadMemoryPhase6Tests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Memory Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Memory Lead",
            phone="+919111111111",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="api",
            display_phone_number="+919876543210",
            phone_number_id="memory-phone-1",
            status="connected",
            is_active=True,
        )
        self.service = LeadMemoryService()
        self._external_counter = 0

    def _message(self, body, *, organization=None, lead=None, account=None):
        organization = organization or self.org
        lead = lead or self.lead
        account = account or self.account
        self._external_counter += 1
        return WhatsAppMessage.objects.create(
            organization=organization,
            account=account,
            lead=lead,
            direction="inbound",
            external_id=f"wamid-memory-{organization.id}-{self._external_counter}",
            from_number=lead.phone,
            to_number=account.display_phone_number,
            body=body,
            status="received",
        )

    def _second_tenant_same_phone(self):
        org = Organization.objects.create(name="Other Memory Org")
        pipeline = Pipeline.objects.create(
            organization=org,
            name="Sales",
            country_code="+91",
            phone_number="9876500000",
        )
        stage = pipeline.stages.get(name="New leads")
        lead = Lead.objects.create(
            organization=org,
            pipeline=pipeline,
            stage=stage,
            name="Same Phone Other Tenant",
            phone=self.lead.phone,
        )
        account = WhatsAppAccount.objects.create(
            organization=org,
            connection_type="api",
            display_phone_number="+919876500000",
            phone_number_id="memory-phone-2",
            status="connected",
            is_active=True,
        )
        return org, lead, account

    def test_memory_identity_is_organization_plus_lead_not_phone(self):
        first = self._message("My budget is ₹50000.")
        self.service.update_from_accepted_turn(
            organization=self.org,
            lead=self.lead,
            source_message_id=first.id,
        )

        other_org, other_lead, other_account = self._second_tenant_same_phone()
        other = self._message(
            "My budget is ₹90000.",
            organization=other_org,
            lead=other_lead,
            account=other_account,
        )
        self.service.update_from_accepted_turn(
            organization=other_org,
            lead=other_lead,
            source_message_id=other.id,
        )

        first_memory = LeadMemory.objects.get(
            organization=self.org,
            lead=self.lead,
        )
        other_memory = LeadMemory.objects.get(
            organization=other_org,
            lead=other_lead,
        )
        self.assertNotEqual(first_memory.id, other_memory.id)
        self.assertEqual(
            first_memory.structured_facts["concept:budget"]["value"],
            "₹50000",
        )
        self.assertEqual(
            other_memory.structured_facts["concept:budget"]["value"],
            "₹90000",
        )

        with self.assertRaises(LeadMemoryScopeError):
            self.service.get_provider_snapshot(
                organization_id=self.org.id,
                lead_id=other_lead.id,
            )

    def test_org_defined_crm_field_is_authoritative_over_lower_confidence_inference(self):
        AttributeDefinition.objects.create(
            organization=self.org,
            name="Budget",
            key="budget",
            field_type="text",
            description="Lead budget",
        )
        self.lead.attributes = {"budget": "₹50000"}
        self.lead.save(update_fields=["attributes", "updated_at"])
        source = self._message("Actually my budget is ₹20000.")

        report = self.service.update_from_accepted_turn(
            organization=self.org,
            lead=self.lead,
            source_message_id=source.id,
        )
        memory = LeadMemory.objects.get(organization=self.org, lead=self.lead)
        fact = memory.structured_facts["attribute:budget"]

        self.assertEqual(fact["value"], "₹50000")
        self.assertEqual(fact["confidence"], 1.0)
        self.assertEqual(fact["source"], "crm_attribute")
        self.assertTrue(
            any(
                item["key"] == "attribute:budget"
                and item.get("reason") == "lower_confidence_or_authority_than_existing_fact"
                for item in report["rejected"]
            )
        )

    def test_validated_qualification_answer_uses_matching_org_defined_field_and_evidence(self):
        AttributeDefinition.objects.create(
            organization=self.org,
            name="Budget",
            key="budget",
            field_type="text",
        )
        org_info = OrgInfo.objects.create(
            organization=self.org,
            qualification_requirements="[id: budget] What is your budget?",
        )
        compiled = compile_qualification_requirements(
            org_info.qualification_requirements
        )
        requirement = compiled["requirements"][0]
        source = self._message("₹75000")
        self.lead.attributes = {
            QUALIFICATION_STATE_KEY: {
                "qualification_status": "in_progress",
                "flow_version": compiled["flow_version"],
                "flow_snapshot": compiled["requirements"],
                "requirement_states": {
                    requirement["id"]: {
                        "status": "answered",
                        "value": "₹75000",
                        "raw_answer": "₹75000",
                        "confidence": "supported",
                        "source_message_id": str(source.id),
                    }
                },
            }
        }
        self.lead.save(update_fields=["attributes", "updated_at"])

        self.service.update_from_accepted_turn(
            organization=self.org,
            lead=self.lead,
            source_message_id=source.id,
        )
        memory = LeadMemory.objects.get(organization=self.org, lead=self.lead)
        fact = memory.structured_facts["attribute:budget"]

        self.assertEqual(fact["value"], "₹75000")
        self.assertEqual(fact["source"], "qualification")
        self.assertEqual(fact["confidence"], 0.95)
        self.assertEqual(fact["field"]["key"], "budget")
        self.assertEqual(fact["evidence"]["message_id"], str(source.id))
        self.assertEqual(fact["evidence"]["text"], "₹75000")

    def test_long_term_events_extend_existing_summary_in_provider_payload(self):
        source = self._message(
            "The price is high, I need to think. "
            "I booked a demo tomorrow. "
            "I will send the documents. "
            "Please connect me to a human agent."
        )
        self.service.update_from_accepted_turn(
            organization=self.org,
            lead=self.lead,
            source_message_id=source.id,
        )

        payload = {
            "conversation_summary": {
                "summary": "Existing rolling summary remains authoritative."
            },
            "recent_conversation": {"messages": [{"body": source.body}]},
        }
        augmented = self.service.augment_provider_payload(
            payload=payload,
            organization_id=self.org.id,
            lead_id=self.lead.id,
        )
        event_types = {
            event["type"]
            for event in augmented["long_term_memory"]["important_events"]
        }

        self.assertEqual(
            augmented["conversation_summary"],
            payload["conversation_summary"],
        )
        self.assertEqual(
            augmented["long_term_memory"]["conversation_summary"],
            payload["conversation_summary"],
        )
        self.assertIn("objection", event_types)
        self.assertIn("appointment", event_types)
        self.assertIn("commitment", event_types)
        self.assertIn("handoff", event_types)
        self.assertIn("recent_conversation", augmented)

    def test_long_term_events_are_idempotent_for_same_message(self):
        source = self._message("I booked a call tomorrow. I will send the details.")
        self.service.update_from_accepted_turn(
            organization=self.org,
            lead=self.lead,
            source_message_id=source.id,
        )
        self.service.update_from_accepted_turn(
            organization=self.org,
            lead=self.lead,
            source_message_id=source.id,
        )
        memory = LeadMemory.objects.get(organization=self.org, lead=self.lead)
        ids = [event["id"] for event in memory.long_term_events]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 2)

    def test_memory_update_is_added_to_ai_trace_without_raw_evidence(self):
        source = self._message("My budget is ₹42000.")
        token = begin_trace(
            organization=self.org,
            lead=self.lead,
            account=self.account,
            source_message=source,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.service.update_from_accepted_turn(
                organization=self.org,
                lead=self.lead,
                source_message_id=source.id,
            )
        finalize_from_result({"status": "completed"})
        flush(reset_token=token)

        trace = AITrace.objects.filter(
            organization=self.org,
            lead=self.lead,
        ).latest("started_at")
        memory_trace = trace.details["memory"]
        self.assertEqual(memory_trace["status"], "updated")
        self.assertEqual(
            memory_trace["identity"],
            {
                "organization_id": str(self.org.id),
                "lead_id": str(self.lead.id),
            },
        )
        self.assertEqual(
            memory_trace["updates"][0]["evidence_message_id"],
            str(source.id),
        )
        serialized = json.dumps(memory_trace, ensure_ascii=False)
        self.assertNotIn("₹42000", serialized)
        self.assertNotIn("My budget", serialized)

    @patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text")
    def test_memory_updates_make_no_ai_provider_call(self, generate_text):
        source = self._message("We receive 35 leads per day.")
        self.service.update_from_accepted_turn(
            organization=self.org,
            lead=self.lead,
            source_message_id=source.id,
        )
        generate_text.assert_not_called()

    def test_source_message_must_match_same_organization_and_lead(self):
        other_org, other_lead, other_account = self._second_tenant_same_phone()
        other_source = self._message(
            "My budget is ₹100000.",
            organization=other_org,
            lead=other_lead,
            account=other_account,
        )
        with self.assertRaises(LeadMemoryScopeError):
            self.service.update_from_accepted_turn(
                organization=self.org,
                lead=self.lead,
                source_message_id=other_source.id,
            )
        self.assertFalse(
            LeadMemory.objects.filter(
                organization=self.org,
                lead=self.lead,
            ).exists()
        )
