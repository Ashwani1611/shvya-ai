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

    def test_exact_transitioning_message_can_finish_after_destination_stage_disables_ai(self):
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
        self.assertTrue(permission.allowed)
        self.assertEqual(
            permission.reason,
            "same_turn_stage_transition_finalization",
        )

        # The destination stage still blocks every later turn.
        later = self._inbound("later-after-transition")
        permission = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=later,
        )
        self.assertFalse(permission.allowed)
        self.assertEqual(permission.reason, "stage_ai_disabled")

    def test_same_turn_stage_exception_does_not_bypass_lead_ai_control(self):
        inbound = self._inbound("transition-lead-disabled")
        self.qualified.ai_on = False
        self.qualified.save(update_fields=["ai_on", "updated_at"])
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
