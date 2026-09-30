from django.test import TestCase

from apps.ai_engagement.services import transactional_turn_runtime as runtime
from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests


class TransactionalDecisionReuseTests(TestCase):
    setUp = AIEngagementControlTests.setUp
    _inbound = AIEngagementControlTests._inbound

    def tearDown(self):
        runtime._PRECOMPUTED_DECISION.set(None)
        super().tearDown()

    def _post_state_marker(self, inbound):
        return {
            "lead_id": str(self.lead.pk),
            "source_message_id": str(inbound.pk),
            "revision": "post-state-regenerate:test-revision",
            "decision": None,
            "force_regenerate": True,
        }

    def test_state_resolution_discards_pre_mutation_response_candidate(self):
        inbound = self._inbound("post-state-regeneration")
        decision = EngagementDecision(
            should_engage=True,
            message="This draft must never be reused after state resolution.",
            file_document_id=None,
            crm_actions=[],
            qualification_updates=[],
            next_requirement_id=None,
            reason="NORMAL_CONVERSATION",
            reason_code="NORMAL_CONVERSATION",
            model="draft-model",
        )

        result = runtime._resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.pk,
            decision=decision,
        )

        self.assertTrue(result["applied"])
        self.assertTrue(result["final_response_requires_regeneration"])
        cached = runtime._PRECOMPUTED_DECISION.get()
        self.assertTrue(cached["force_regenerate"])
        self.assertIsNone(cached["decision"])
        self.assertTrue(
            str(cached["revision"]).startswith("post-state-regenerate:")
        )

    def test_exact_transitioning_message_respects_destination_stage_ai_off(self):
        inbound = self._inbound("transition-finalization")
        self.qualified.ai_on = False
        self.qualified.save(update_fields=["ai_on", "updated_at"])
        self.lead.stage = self.qualified
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()

        runtime._PRECOMPUTED_DECISION.set(self._post_state_marker(inbound))

        permission = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=inbound,
        )
        self.assertFalse(permission.allowed)
        self.assertEqual(permission.reason, "stage_ai_disabled")

        # The destination stage still blocks every later turn.
        later = self._inbound("later-after-transition")
        permission = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=later,
        )
        self.assertFalse(permission.allowed)
        self.assertEqual(permission.reason, "stage_ai_disabled")

    def test_same_turn_marker_does_not_bypass_lead_ai_control(self):
        inbound = self._inbound("transition-lead-disabled")
        self.lead.stage = self.qualified
        self.lead.ai_enabled = False
        self.lead.save(update_fields=["stage", "ai_enabled", "updated_at"])
        self.lead.refresh_from_db()

        runtime._PRECOMPUTED_DECISION.set(self._post_state_marker(inbound))

        permission = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=inbound,
        )
        self.assertFalse(permission.allowed)
        self.assertEqual(permission.reason, "lead_ai_disabled")

    def test_final_generation_input_contains_committed_stage_and_file_action(self):
        import json

        from apps.ai_engagement.models import Document
        from apps.ai_engagement.services.context import AIContextBuilder
        from apps.ai_engagement.services.engagement import EngagementService
        from apps.ai_engagement.services.runtime_state import STATE_KEY
        from apps.crm.models import Stage

        source = self._inbound("stage-and-file-input")
        self.lead.stage = self.qualified
        self.lead.save(update_fields=["stage", "updated_at"])
        destination = Stage.objects.create(pipeline=self.pipeline, name="Demo Requested", display_order=90)
        document = Document.objects.create(
            organization=self.organization, name="Product guide", file="guides/product.pdf",
            share_instruction="Share when the lead asks for a guide.", processing_status="completed",
        )
        decision = EngagementDecision(
            should_engage=True, message="Draft response", file_document_id=document.pk,
            crm_actions=[{"type": "pipeline_transition", "stage_shift": {"stage_id": str(destination.pk)}}],
            reason="NORMAL_CONVERSATION", model="test",
        )
        result = runtime._resolve_state_before_response(
            organization=self.organization, lead=self.lead, source_message_id=source.pk, decision=decision,
        )
        self.assertTrue(result["applied"])
        self.lead.refresh_from_db()
        context = AIContextBuilder().build(organization=self.organization, lead=self.lead)
        self.assertNotIn(STATE_KEY, context.lead["attributes"])
        payload = json.loads(EngagementService()._build_input(context=context))
        resolved = payload["operational_state"]["resolved_actions"]
        self.assertEqual(resolved["source_message_id"], str(source.pk))
        self.assertIn("pipeline_transition", resolved["action_types"])
        self.assertIn("file_share", resolved["action_types"])
        self.assertEqual(resolved["file_share"], {"status": "resolved_pending_send", "document_name": document.name})
        self.assertEqual(context.stage["id"], str(destination.pk))

        self._inbound("later-unrelated-source")
        later_context = AIContextBuilder().build(organization=self.organization, lead=self.lead)
        later_payload = json.loads(EngagementService()._build_input(context=later_context))
        self.assertEqual(later_payload["operational_state"]["resolved_actions"]["action_types"], [])
        self.assertNotIn("file_share", later_payload["operational_state"]["resolved_actions"])

    def test_final_generation_input_cannot_load_another_tenants_reminders(self):
        import json

        from django.utils import timezone

        from apps.ai_engagement.services.context import AIContextBuilder
        from apps.ai_engagement.services.engagement import EngagementService
        from apps.crm.models import LeadReminder
        from apps.organizations.models import Organization

        self._inbound("tenant-scoped-operational-state")
        LeadReminder.objects.create(lead=self.lead, title="Private reminder", due_at=timezone.now())
        context = AIContextBuilder().build(organization=self.organization, lead=self.lead)
        context.organization["id"] = str(Organization.objects.create(name="Other context tenant").pk)
        payload = json.loads(EngagementService()._build_input(context=context))
        self.assertNotIn("operational_state", payload)
        self.assertNotIn("Private reminder", json.dumps(payload))
