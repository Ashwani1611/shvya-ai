from django.test import TestCase

from apps.ai_engagement.services import transactional_turn_runtime as runtime
from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests


class TransactionalDecisionReuseTests(TestCase):
    setUp = AIEngagementControlTests.setUp
    _inbound = AIEngagementControlTests._inbound

    def tearDown(self):
        runtime._PRECOMPUTED_DECISION.set(None)
        super().tearDown()

    def test_exact_transitioning_message_can_finish_after_destination_stage_disables_ai(self):
        inbound = self._inbound("transition-finalization")
        self.qualified.ai_on = False
        self.qualified.save(update_fields=["ai_on", "updated_at"])
        self.lead.stage = self.qualified
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()

        runtime._PRECOMPUTED_DECISION.set(
            {
                "lead_id": str(self.lead.pk),
                "source_message_id": str(inbound.pk),
                "revision": "test-revision",
                "decision": object(),
            }
        )

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

        runtime._PRECOMPUTED_DECISION.set(
            {
                "lead_id": str(self.lead.pk),
                "source_message_id": str(inbound.pk),
                "revision": "test-revision",
                "decision": object(),
            }
        )

        permission = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
            latest_inbound=inbound,
        )
        self.assertFalse(permission.allowed)
        self.assertEqual(permission.reason, "lead_ai_disabled")
