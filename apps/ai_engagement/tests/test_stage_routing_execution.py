from types import SimpleNamespace

from django.test import TestCase

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.models import AIActionReceipt, OrgInfo
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.engagement_instruction_policy import compile_engagement_instruction_policy
from apps.ai_engagement.services import trace_service
from apps.ai_engagement.services import transactional_turn_runtime as runtime
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline, Stage


class StageRoutingExecutionTests(TestCase):
    """Exercise the graph policy and real source-bound CRM mutation together."""

    def setUp(self):
        AIEngagementControlTests.setUp(self)
        self.lead.stage = self.qualified
        self.lead.save(update_fields=["stage", "updated_at"])
        self.destination = Stage.objects.create(
            pipeline=self.pipeline, name="Human Intervention", display_order=90,
            description="Move here when the lead wants human support.",
        )
        self.info = OrgInfo.objects.create(
            organization=self.organization,
            ai_playbook="## Stage shifting logic\nIf the lead wants human support, move to Human Intervention.",
        )

    def message(self, body, *, direction="inbound", status=None, account=None):
        return WhatsAppMessage.objects.create(
            organization=self.organization, account=account or self.account, lead=self.lead,
            direction=direction, body=body,
            status=status or ("received" if direction == "inbound" else "sent"),
            from_number=self.lead.phone, to_number=self.account.display_phone_number,
        )

    def tearDown(self):
        runtime._PRECOMPUTED_DECISION.set(None)
        super().tearDown()

    def proposal(self):
        return {"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.destination.pk)}}

    def execute(self, source, *, use_policy=True, transactional=False, document_id=None):
        self.lead.refresh_from_db()
        actions = [self.proposal()]
        if use_policy:
            context = AIContextBuilder().build(organization=self.organization, lead=self.lead)
            actions, _ = build_controlled_actions(
                decision=SimpleNamespace(crm_actions=actions, qualification_updates=[]),
                context=context,
                runtime_policy={"qualification": {"criteria": []}, "crm": compile_engagement_instruction_policy(self.info.ai_playbook)},
                qualification_state={"engagement_mode": "conversation", "requirement_states": {}},
                requirements=[],
            )
        token = trace_service.begin_trace(
            organization=self.organization, lead=self.lead, source_message=source, account=self.account,
        )
        try:
            if transactional:
                return runtime._resolve_state_before_response(
                    organization=self.organization, lead=self.lead,
                    source_message_id=source.pk, account_id=self.account.pk,
                    decision=EngagementDecision(
                        should_engage=True, message="I can help with that.", file_document_id=document_id,
                        crm_actions=actions, reason="NORMAL_CONVERSATION", model="test",
                    ),
                )
            return CRMActionExecutor().execute(
                organization=self.organization, lead=self.lead, actions=actions, source_message=source,
            )
        finally:
            trace_service.flush(reset_token=token)

    def test_confirmation_reaches_crm_and_duplicate_replays_receipt(self):
        self.message("Would you like human support?", direction="outbound")
        source = self.message("Yes")
        results = self.execute(source)
        self.assertEqual(len(results), 1)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.destination.pk)
        replay = self.execute(source, use_policy=False)
        self.assertTrue(replay[0]["idempotent_replay"])
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)

    def test_pipeline_description_route_reaches_crm(self):
        target_pipeline = Pipeline.objects.create(
            organization=self.organization, name="Enterprise",
            description="Customers requesting enterprise onboarding.",
        )
        self.destination = Stage.objects.create(pipeline=target_pipeline, name="Ready", display_order=90)
        self.info.ai_playbook = ""
        self.info.save(update_fields=["ai_playbook"])
        self.execute(self.message("We need enterprise onboarding"))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.destination.pk)
        self.assertEqual(self.lead.pipeline_id, target_pipeline.pk)

    def test_completed_qualification_allows_later_stage_and_file_in_same_turn(self):
        from apps.ai_engagement.models import Document
        from apps.ai_engagement.services.qualification_state import state_for_lead
        from apps.ai_engagement.services.runtime_state import STATE_KEY

        self.assertEqual(state_for_lead(self.lead)["qualification_status"], "completed")
        document = Document.objects.create(
            organization=self.organization, name="Support guide", file="guides/support.pdf",
            processing_status="completed", share_instruction="Share when the lead requests human support.",
        )
        source = self.message("I want human support")
        result = self.execute(source, transactional=True, document_id=document.pk)
        self.assertTrue(result["applied"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.destination.pk)
        self.assertEqual(state_for_lead(self.lead)["qualification_status"], "completed")
        state = self.lead.attributes[STATE_KEY]
        self.assertEqual(state["pre_resolved_file_document_id"], document.pk)
        self.assertEqual(set(state["pre_resolved_actions"]), {"pipeline_transition", "file_share"})
        duplicate = self.execute(source, transactional=True, use_policy=False, document_id=document.pk)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)

        self.destination = Stage.objects.create(pipeline=self.pipeline, name="Demo Requested", display_order=91)
        self.info.ai_playbook = "## Stage shifting logic\nIf the lead requests a demo, move to Demo Requested."
        self.info.save(update_fields=["ai_playbook"])
        followup = self.execute(self.message("I want a demo"), transactional=True)
        self.assertTrue(followup["applied"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.destination.pk)
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 2)

    def test_confirmation_cannot_use_other_account_or_future_reply(self):
        other_account = WhatsAppAccount.objects.create(
            organization=self.organization, business_name="Other sender", status="connected",
        )
        self.message("Would you like human support?", direction="outbound", account=other_account)
        source = self.message("Yes")
        self.message("Would you like human support?", direction="outbound")
        self.assertEqual(self.execute(source, use_policy=False), [])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.qualified.pk)

    def test_failed_prompt_and_negative_answer_never_move_stage(self):
        for prompt_status, reply in [("failed", "Yes"), ("queued", "Yes"), ("sent", "No")]:
            with self.subTest(prompt_status=prompt_status, reply=reply):
                self.message("Would you like human support?", direction="outbound", status=prompt_status)
                self.assertEqual(self.execute(self.message(reply)), [])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.qualified.pk)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())

    def test_explicit_rule_cannot_be_overridden_by_broader_description(self):
        self.info.ai_playbook = "## Stage shifting logic\nIf the lead wants human support and budget approved, move to Human Intervention."
        self.info.save(update_fields=["ai_playbook"])
        self.assertEqual(self.execute(self.message("I want human support"), use_policy=False), [])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.qualified.pk)
