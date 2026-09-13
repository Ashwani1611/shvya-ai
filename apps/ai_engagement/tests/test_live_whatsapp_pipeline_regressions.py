import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.engagement import EngagementService
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.crm.views.api import BulkMoveStageAPIView
from apps.organizations.models import Organization
from services.channels.ai_orchestration_hooks import (
    _conversation_bound_hosted_block_reason,
)


class _InvalidQualificationProvider:
    """Return schema-shaped output that deliberately violates backend sequencing."""

    def generate_text(self, **kwargs):
        payload = {
            "should_engage": True,
            "silence_rule": None,
            "message": "Hello",
            "file_document_id": None,
            "crm_actions": [],
            "qualification_updates": [],
            "next_requirement_id": "not-a-real-requirement",
            "reason_code": "QUALIFICATION_NEXT",
        }
        return AITextResult(text=json.dumps(payload), model="invalid-test-model")


class LiveWhatsAppPipelineRegressionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Live Regression Org")
        self.sales = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
            ai_enabled=True,
        )
        self.leads = Pipeline.objects.create(
            organization=self.organization,
            name="Leads 2",
            country_code="+91",
            phone_number="8888888888",
            ai_enabled=True,
        )
        self.sales_stage = self.sales.stages.get(name="New leads")
        self.leads_stage = self.leads.stages.get(name="New leads")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.sales,
            stage=self.sales_stage,
            name="Customer",
            phone="+919111111111",
            ai_enabled=True,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Sales API",
            phone_number_id="meta-sales",
            display_phone_number="+919876543210",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _inbound(self, external_id, body="Hello"):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=external_id,
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body=body,
            status=WhatsAppMessage.Status.RECEIVED,
        )

    def test_established_conversation_survives_pipeline_move_without_prior_ai_reply(self):
        self._inbound("before-pipeline-move", "First customer message")
        self.lead.pipeline = self.leads
        self.lead.stage = self.leads_stage
        self.lead.save(update_fields=["pipeline", "stage", "updated_at"])
        self._inbound("after-pipeline-move", "Second customer message")

        self.lead.refresh_from_db()
        decision = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_hosted_permission_uses_its_exact_inbound_even_if_other_number_is_newer(self):
        hosted = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Sales Hosted",
            phone_number_id="+919876543210",
            display_phone_number="+919876543210",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=hosted,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="hosted-owned-turn",
            from_number=self.lead.phone,
            to_number=hosted.display_phone_number,
            body="Hosted question",
            status=WhatsAppMessage.Status.RECEIVED,
        )
        # A later inbound on another connected number must not steal the durable
        # Hosted job's permission context.
        self._inbound("newer-api-turn", "API question")

        reason = _conversation_bound_hosted_block_reason(
            account=hosted,
            lead=self.lead,
        )

        self.assertEqual(reason, "")

    def test_pipeline_context_marks_persisted_pipeline_as_current_and_candidates_internal(self):
        self.lead.pipeline = self.leads
        self.lead.stage = self.leads_stage
        self.lead.save(update_fields=["pipeline", "stage", "updated_at"])
        self.lead.refresh_from_db()

        context = AIContextBuilder()._build_pipeline_context(lead=self.lead)

        self.assertEqual(context["name"], "Leads 2")
        self.assertEqual(context["current_pipeline"]["id"], str(self.leads.id))
        self.assertEqual(context["current_pipeline"]["name"], "Leads 2")
        self.assertTrue(context["routing_metadata_internal_only"])
        self.assertTrue(
            all(item.get("customer_visible") is False for item in context["available_stages"])
        )

    def test_invalid_model_qualification_output_falls_back_to_backend_question(self):
        OrgInfo.objects.update_or_create(
            organization=self.organization,
            defaults={
                "qualification_requirements": "What is your budget?",
                "ai_enabled": True,
            },
        )
        self._inbound("invalid-provider-turn", "Hi")

        # Production workers leave EngagementService.provider unset and resolve
        # OpenAIProvider internally. Patch that factory so this regression covers
        # the real fail-soft boundary without weakening strict injected-provider
        # validation tests elsewhere.
        with patch(
            "apps.ai_engagement.services.engagement.OpenAIProvider",
            return_value=_InvalidQualificationProvider(),
        ):
            decision = EngagementService().engage(
                organization=self.organization,
                lead=self.lead,
            )

        self.assertTrue(decision.should_engage)
        self.assertEqual(decision.model, "deterministic-fallback")
        self.assertEqual(decision.message, "What is your budget?")
        self.assertEqual(decision.crm_actions, [])
        self.assertIsNotNone(decision.next_requirement_id)

    def test_bulk_api_cross_pipeline_move_persists_pipeline_and_stage(self):
        api_key = SimpleNamespace(
            can_upsert_leads=True,
            organization=self.organization,
        )
        request = SimpleNamespace(
            auth=api_key,
            user=SimpleNamespace(pk=None),
            data={
                "lead_ids": [str(self.lead.id)],
                "pipeline": self.leads.name,
                "stage": self.leads_stage.name,
            },
        )

        response = BulkMoveStageAPIView().post(request)

        self.assertEqual(response.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.pipeline_id, self.leads.id)
        self.assertEqual(self.lead.stage_id, self.leads_stage.id)
        self.assertEqual(response.data["pipeline"], self.leads.name)

    def test_deleting_lead_permanently_deletes_linked_whatsapp_messages(self):
        message = self._inbound("delete-with-lead", "Delete this conversation")
        message_id = message.id
        lead_id = self.lead.id

        self.lead.delete()

        self.assertFalse(Lead.objects.filter(pk=lead_id).exists())
        self.assertFalse(WhatsAppMessage.objects.filter(pk=message_id).exists())
