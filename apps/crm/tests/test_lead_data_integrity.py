from django.apps import apps
from django.db import models
from django.test import TestCase
from django.utils import timezone

from apps.ai_engagement.models import (
    AIActionReceipt,
    AITrace,
    InternalConversationSummary,
    LeadSignal,
)
from apps.copilot.models import CopilotLeadFlag
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import (
    Lead,
    LeadActivity,
    LeadCall,
    LeadNote,
    LeadReminder,
    Pipeline,
    Stage,
)
from apps.followups.models import FollowupSenderState
from apps.organizations.models import Organization
from services.crm.lead_service import create_lead, upsert_lead
from services.crm.lead_transition import move_lead_to_pipeline_stage


class LeadDataIntegrityTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Lead Integrity Org")
        self.pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Leads",
        )
        self.stage = self.pipeline.stages.get(name="Qualified")
        self.lead = create_lead(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Integrity Lead",
            phone="+919999999991",
            email="lead@example.com",
            notes="Inline lead note",
            attributes={"budget": "50000", "city": "Delhi"},
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Integrity API",
            phone_number_id="integrity-phone-id",
            display_phone_number="+919888888888",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def test_permanent_lead_delete_removes_owned_data_but_keeps_shared_pipeline_config(self):
        note = LeadNote.objects.create(
            lead=self.lead,
            note="Manual note",
        )
        reminder = LeadReminder.objects.create(
            lead=self.lead,
            title="Call back",
            due_at=timezone.now(),
        )
        call = LeadCall.objects.create(
            lead=self.lead,
            status="completed",
            duration_seconds=30,
            called_at=timezone.now(),
        )
        summary = InternalConversationSummary.objects.create(
            organization=self.organization,
            lead=self.lead,
            summary="Internal AI summary",
        )
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="lead-integrity-delete-message",
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body="Delete this with the lead",
            status=WhatsAppMessage.Status.RECEIVED,
        )
        sender_state = FollowupSenderState.objects.create(
            account=self.account,
            last_lead=self.lead,
        )

        # Intent score is Lead-owned state, while its supporting AI observations,
        # receipts, traces and Co-Pilot flags are separate durable rows. A CRM
        # deletion must remove the whole lead-owned AI footprint.
        attributes = dict(self.lead.attributes or {})
        attributes["_shvya_ai_intent_score"] = {
            "version": 4,
            "assessed": True,
            "score": 9,
            "max_score": 10,
        }
        Lead.objects.filter(pk=self.lead.pk).update(attributes=attributes)
        self.lead.refresh_from_db()

        signal = LeadSignal.objects.create(
            organization=self.organization,
            lead=self.lead,
            source_message_id=message.pk,
            kind="intent",
            detail="high",
        )
        receipt = AIActionReceipt.objects.create(
            organization=self.organization,
            lead=self.lead,
            source_message_id=message.pk,
            idempotency_key="lead-delete-intent",
            action_type="intent_test",
            result={"score": 9},
        )
        trace = AITrace.objects.create(
            organization=self.organization,
            lead=self.lead,
            source_inbound_message_id=message.pk,
            connection_type=AITrace.ConnectionType.API,
            status=AITrace.Status.COMPLETED,
        )
        flag = CopilotLeadFlag.objects.create(
            organization=self.organization,
            lead=self.lead,
            flag_code=CopilotLeadFlag.FlagCode.HIGH_INTENT_NO_ACTION,
            severity=CopilotLeadFlag.Severity.HIGH,
        )

        lead_id = self.lead.id
        pipeline_id = self.pipeline.id
        stage_id = self.stage.id
        activity_ids = list(
            LeadActivity.objects.filter(lead=self.lead).values_list("id", flat=True)
        )

        # Confirm the inline Lead-owned values exist before the row is removed.
        stored = Lead.objects.get(pk=lead_id)
        self.assertEqual(stored.notes, "Inline lead note")
        self.assertEqual(stored.attributes["budget"], "50000")

        self.lead.delete()

        self.assertFalse(Lead.objects.filter(pk=lead_id).exists())
        self.assertFalse(LeadNote.objects.filter(pk=note.pk).exists())
        self.assertFalse(LeadReminder.objects.filter(pk=reminder.pk).exists())
        self.assertFalse(LeadCall.objects.filter(pk=call.pk).exists())
        self.assertFalse(
            InternalConversationSummary.objects.filter(pk=summary.pk).exists()
        )
        self.assertFalse(WhatsAppMessage.objects.filter(pk=message.pk).exists())
        self.assertFalse(LeadSignal.objects.filter(pk=signal.pk).exists())
        self.assertFalse(AIActionReceipt.objects.filter(pk=receipt.pk).exists())
        self.assertFalse(AITrace.objects.filter(pk=trace.pk).exists())
        self.assertFalse(CopilotLeadFlag.objects.filter(pk=flag.pk).exists())
        self.assertFalse(
            LeadActivity.objects.filter(pk__in=activity_ids).exists()
        )

        # Sender throttling belongs to the WhatsApp account, not to one Lead.
        # Keep the shared throttle row but erase its pointer to the deleted Lead.
        sender_state.refresh_from_db()
        self.assertIsNone(sender_state.last_lead_id)

        # Pipeline and Stage are shared organization configuration, not data
        # owned by one Lead, so deleting a Lead must never delete them.
        self.assertTrue(Pipeline.objects.filter(pk=pipeline_id).exists())
        self.assertTrue(Stage.objects.filter(pk=stage_id).exists())

    def test_every_non_cascade_lead_relation_has_an_explicit_deletion_rule(self):
        """Prevent future models from silently leaving orphaned Lead data.

        WhatsAppMessage is a historical SET_NULL relation but the Lead
        pre_delete receiver hard-deletes its rows. FollowupSenderState is
        account-owned throttling state; deleting a Lead intentionally clears
        only its last_lead pointer. CampaignDelivery is campaign-owned delivery
        evidence; deleting a Lead clears its pointer but preserves the frozen
        recipient and attempt history. SalesDocument is legal/commercial
        evidence with a frozen recipient snapshot, so deleting the CRM Lead
        clears only its optional CRM pointer. JustDialLeadEvent is provider
        ingestion/audit evidence whose optional CRM pointer is cleared.
        IndiaMART receipts retain only their query ID after buyer payload
        erasure to prevent retries from recreating deleted leads. Every other
        Lead-owned relation must CASCADE.

        """
        exceptions = []

        for model in apps.get_models():
            for field in model._meta.fields:
                remote_field = getattr(field, "remote_field", None)
                if not remote_field or remote_field.model is not Lead:
                    continue
                if remote_field.on_delete is models.CASCADE:
                    continue
                exceptions.append(
                    (model._meta.label_lower, field.name, remote_field.on_delete)
                )

        self.assertEqual(
            {(label, field_name) for label, field_name, _ in exceptions},
            {
                ("channels.campaigndelivery", "lead"),
                ("channels.whatsappmessage", "lead"),
                # Account-owned provider history survives, with its CRM link cleared.
                ("channels.instagramconversation", "lead"),
                ("followups.followupsenderstate", "last_lead"),
                # Provider ingestion evidence survives, with its CRM link cleared.
                ("integrations.justdialleadevent", "lead"),
                # Commercial/legal documents survive CRM lead deletion.
                ("sales.salesdocument", "lead"),
                # Enquiry tombstones survive; pre_delete erases buyer payloads.
                ("integrations.indiamartreceipt", "lead"),
                # 99acres receipts and minimal audit events remain as
                # privacy-safe deduplication evidence after CRM lead deletion.
                ("integrations.acres99receipt", "lead"),
                ("integrations.acres99event", "lead"),
            },
        )
        self.assertTrue(
            all(on_delete is models.SET_NULL for _, _, on_delete in exceptions)
        )

    def test_create_lead_is_persisted_with_pipeline_stage_and_creation_history(self):
        created = create_lead(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Created Backend Lead",
            phone="+919999999992",
            attributes={"source": "test"},
        )

        stored = Lead.objects.get(pk=created.pk)
        self.assertEqual(stored.pipeline_id, self.pipeline.id)
        self.assertEqual(stored.stage_id, self.stage.id)
        self.assertEqual(stored.attributes, {"source": "test"})
        self.assertTrue(
            LeadActivity.objects.filter(
                lead=stored,
                topic=LeadActivity.Topic.LEAD_CREATED,
                new_pipeline=self.pipeline,
                new_stage=self.stage,
            ).exists()
        )

    def test_cross_pipeline_move_persists_row_and_pipeline_history_atomically(self):
        destination = Pipeline.objects.create(
            organization=self.organization,
            name="Destination",
            is_active=True,
        )
        destination_stage = destination.stages.get(name="Qualified")

        move_lead_to_pipeline_stage(
            lead=self.lead,
            pipeline=destination,
            stage=destination_stage,
            actor=None,
        )

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.pipeline_id, destination.id)
        self.assertEqual(self.lead.stage_id, destination_stage.id)
        self.assertTrue(
            LeadActivity.objects.filter(
                lead=self.lead,
                topic=LeadActivity.Topic.PIPELINE_CHANGED,
                old_pipeline=self.pipeline,
                new_pipeline=destination,
                old_stage=self.stage,
                new_stage=destination_stage,
            ).exists()
        )

    def test_existing_lead_upsert_uses_backend_transition_and_persists_history(self):
        destination = Pipeline.objects.create(
            organization=self.organization,
            name="API Destination",
            is_active=True,
        )
        destination_stage = destination.stages.get(name="Qualified")

        updated, created = upsert_lead(
            organization=self.organization,
            pipeline=destination,
            stage=destination_stage,
            name="Integrity Lead Updated",
            phone=self.lead.phone,
            email="updated@example.com",
            attributes={"budget": "75000"},
            lead_source="external_api",
        )

        self.assertFalse(created)
        updated.refresh_from_db()
        self.assertEqual(updated.pipeline_id, destination.id)
        self.assertEqual(updated.stage_id, destination_stage.id)
        self.assertEqual(updated.email, "updated@example.com")
        self.assertEqual(updated.attributes["budget"], "75000")
        self.assertTrue(
            LeadActivity.objects.filter(
                lead=updated,
                topic=LeadActivity.Topic.PIPELINE_CHANGED,
                old_pipeline=self.pipeline,
                new_pipeline=destination,
                old_stage=self.stage,
                new_stage=destination_stage,
            ).exists()
        )


    def test_upsert_normalizes_phone_before_lookup(self):
        updated, created = upsert_lead(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Formatting Variant",
            phone="+91-99999-99991",
            email="normalized@example.com",
            lead_source="external_api",
            send_welcome=False,
        )

        self.assertFalse(created)
        self.assertEqual(updated.pk, self.lead.pk)
        updated.refresh_from_db()
        self.assertEqual(updated.phone, "+919999999991")
        self.assertEqual(updated.email, "normalized@example.com")
        self.assertEqual(
            Lead.objects.filter(
                organization=self.organization,
                phone="+919999999991",
            ).count(),
            1,
        )
