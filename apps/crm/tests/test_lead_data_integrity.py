from django.apps import apps
from django.db import models
from django.test import TestCase
from django.utils import timezone

from apps.ai_engagement.models import InternalConversationSummary
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
        self.assertFalse(
            LeadActivity.objects.filter(pk__in=activity_ids).exists()
        )

        # Pipeline and Stage are shared organization configuration, not data
        # owned by one Lead, so deleting a Lead must never delete them.
        self.assertTrue(Pipeline.objects.filter(pk=pipeline_id).exists())
        self.assertTrue(Stage.objects.filter(pk=stage_id).exists())

    def test_every_non_cascade_lead_relation_has_an_explicit_hard_delete_path(self):
        """Prevent future models from silently leaving orphaned Lead data.

        WhatsAppMessage is the one historical SET_NULL relation; the Lead
        pre_delete receiver hard-deletes those rows. Any new Lead relation must
        use CASCADE unless an equally explicit product deletion path is added.
        """
        exceptions = []

        for model in apps.get_models():
            for field in model._meta.fields:
                remote_field = getattr(field, "remote_field", None)
                if not remote_field or remote_field.model is not Lead:
                    continue
                if remote_field.on_delete is models.CASCADE:
                    continue
                exceptions.append((model, field.name, remote_field.on_delete))

        self.assertEqual(
            [(model, field_name) for model, field_name, _ in exceptions],
            [(WhatsAppMessage, "lead")],
        )
        self.assertIs(exceptions[0][2], models.SET_NULL)

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
