from datetime import timedelta
import json
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.ai_engagement.models import AIActionReceipt, OrgInfo
from apps.ai_engagement.services.ai_provider import AIProviderTransientError, AITextResult
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY
from apps.ai_engagement.services.transactional_decision_reuse import operational_state_for_context
from apps.channels.instagram_models import InstagramMessage
from apps.channels.tests import test_instagram_ai as fixtures
from apps.crm.models import Lead, LeadReminder
from services.channels.instagram_ai import InstagramAIContextBuilder, execute_instagram_ai_engagement


class InstagramPostStateResponseTests(TransactionTestCase):
    task = fixtures.InstagramAIEngagementTests.task

    def setUp(self):
        fixtures.InstagramAIEngagementTests.setUp(self)
        self.inbound.body = "Please call me tomorrow at 3 PM"
        self.inbound.save(update_fields=["body"])

    @override_settings(OPENAI_API_KEY="")
    @patch("services.channels.instagram_ai._dispatch_instagram_ai_message")
    @patch("services.channels.instagram_inbox.assert_reply_allowed")
    @patch("apps.ai_engagement.services.engagement.OpenAIProvider")
    def test_real_graph_accepts_its_own_deterministic_answer_revision(self, provider_class, allowed, dispatch):
        from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
        from apps.ai_engagement.services.qualification_state import record_last_asked_requirement, state_for_lead

        questions = (
            "[id: lead_system] Where do you manage leads?\nA. WhatsApp\nB. Excel\n\n"
            "[id: lead_volume] How many leads do you receive?\nA. 0-10\nB. 10-30"
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.org)
        info.ai_playbook = "## Qualification Questions\n" + questions
        info.save(update_fields=["ai_playbook"])
        requirements = compile_qualification_requirements(questions)["requirements"]
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        self.inbound.body = "A"
        self.inbound.save(update_fields=["body"])

        def compose(**kwargs):
            self.assertTrue(_FINAL_LANGUAGE_ONLY.get())
            self.assertFalse(connection.in_atomic_block)
            self.lead.refresh_from_db()
            committed = state_for_lead(self.lead, requirements=requirements)
            self.assertEqual(committed["requirement_states"][requirements[0]["id"]]["value"], "WhatsApp")
            payload = json.loads(kwargs["input_text"])
            self.assertEqual(payload["response_plan"]["phase"], "FINAL_COMPOSITION")
            self.assertEqual(payload["next_requirement"]["id"], requirements[1]["id"])
            return AITextResult(json.dumps({
                "should_engage": True, "silence_rule": None,
                "message": requirements[1]["question"], "file_document_id": None,
                "crm_actions": [], "qualification_updates": [],
                "next_requirement_id": requirements[1]["id"], "reason_code": "QUALIFICATION_NEXT",
            }), "recorded-final-response")

        provider_class.return_value.generate_text.side_effect = compose
        result = execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)

        self.assertEqual(result["status"], "completed")
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(state["requirement_states"][requirements[0]["id"]]["value"], "WhatsApp")
        self.assertEqual(state["requirement_states"][requirements[0]["id"]]["status"], "answered")
        outbound = InstagramMessage.objects.get(conversation=self.conversation, direction="outbound")
        self.assertIn("How many leads", outbound.body)
        provider_class.return_value.generate_text.assert_called_once()
        dispatch.assert_called_once()

    def decision(self, *, final=False):
        return EngagementDecision(
            should_engage=True,
            message="Your callback reminder is saved." if final else "Uncommitted draft must not be sent.",
            file_document_id=None,
            crm_actions=[{
                "type": "create_reminder", "title": "Call lead",
                "description": "Lead asked for a callback.",
                "due_at": (timezone.now() + timedelta(days=1)).isoformat(),
            }],
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="test",
        )

    @patch("services.channels.instagram_ai._dispatch_instagram_ai_message")
    @patch("services.channels.instagram_inbox.assert_reply_allowed")
    @patch("services.channels.instagram_ai.EngagementService.engage")
    def test_reply_is_generated_after_committed_reminder_and_final_proposals_are_ignored(self, engage, allowed, dispatch):
        phases = []

        def generate(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            final = _FINAL_LANGUAGE_ONLY.get()
            phases.append(final)
            if final:
                self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
                context = InstagramAIContextBuilder(conversation_id=self.conversation.pk).build(
                    organization=self.org, lead=kwargs["lead"],
                )
                operational = operational_state_for_context(context)
                self.assertEqual(operational["resolved_actions"]["source_message_id"], str(self.inbound.pk))
                self.assertIn("create_reminder", operational["resolved_actions"]["action_types"])
                self.assertEqual(operational["pending_reminders"][0]["title"], "Call lead")
            return self.decision(final=final)

        engage.side_effect = generate
        result = execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(phases, [False, True])
        self.assertFalse(_FINAL_LANGUAGE_ONLY.get())
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
        self.assertEqual(AIActionReceipt.objects.filter(organization=self.org, lead=self.lead).count(), 1)
        outbound = InstagramMessage.objects.get(conversation=self.conversation, direction="outbound")
        self.assertEqual(outbound.body, "Your callback reminder is saved.")
        self.inbound.refresh_from_db()
        self.assertTrue(self.inbound.raw_payload["shvya_ai_processing"]["state_resolved"])
        self.assertTrue(self.inbound.raw_payload["shvya_ai_processing"]["processed"])

    @patch("services.channels.instagram_ai._dispatch_instagram_ai_message")
    @patch("services.channels.instagram_inbox.assert_reply_allowed")
    @patch("services.channels.instagram_ai.EngagementService.engage")
    def test_provider_retry_resumes_final_pass_without_repeating_reminder(self, engage, allowed, dispatch):
        engage.side_effect = [self.decision(), AIProviderTransientError("temporary provider failure")]
        with self.assertRaisesRegex(AssertionError, "Unexpected Instagram AI retry"):
            execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
        self.assertFalse(InstagramMessage.objects.filter(conversation=self.conversation, direction="outbound").exists())
        self.assertFalse(_FINAL_LANGUAGE_ONLY.get())

        def final(**kwargs):
            self.assertTrue(_FINAL_LANGUAGE_ONLY.get())
            self.assertFalse(connection.in_atomic_block)
            return self.decision(final=True)

        engage.reset_mock()
        engage.side_effect = final
        result = execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)
        self.assertEqual(result["status"], "completed")
        engage.assert_called_once()
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
        self.assertEqual(AIActionReceipt.objects.filter(organization=self.org, lead=self.lead).count(), 1)
        self.assertEqual(InstagramMessage.objects.filter(conversation=self.conversation, direction="outbound").count(), 1)

    @patch("services.channels.instagram_ai._dispatch_instagram_ai_message")
    @patch("services.channels.instagram_inbox.assert_reply_allowed")
    @patch("services.channels.instagram_ai.EngagementService.engage")
    def test_external_stage_change_during_final_generation_does_not_send_stale_reply(self, engage, allowed, dispatch):
        other_stage = self.pipeline.stages.exclude(pk=self.stage.pk).first()
        other_stage.ai_on = True
        other_stage.save(update_fields=["ai_on"])

        def generate(**kwargs):
            final = _FINAL_LANGUAGE_ONLY.get()
            if final:
                Lead.objects.filter(pk=self.lead.pk, organization=self.org).update(stage=other_stage)
            return self.decision(final=final)

        engage.side_effect = generate
        task = self.task()
        with self.assertRaisesRegex(AssertionError, "Unexpected Instagram AI retry"):
            execute_instagram_ai_engagement(task=task, message_id=self.inbound.pk)
        self.assertIn("Lead state changed before Instagram reply", str(task.retry.call_args.kwargs["exc"]))
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), 1)
        self.assertFalse(InstagramMessage.objects.filter(conversation=self.conversation, direction="outbound").exists())
        dispatch.assert_not_called()
