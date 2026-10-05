import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import EngagementDecision, EngagementError
from apps.ai_engagement.services.final_reply_language import finalize_reply_language, requested_language
from apps.ai_engagement.services.qualification_state import _non_answer_evidence


class FinalReplyLanguageTests(SimpleTestCase):
    def test_runtime_reuses_generated_context_without_second_build(self):
        from apps.ai_engagement.services import final_reply_language as runtime
        from apps.ai_engagement.services.engagement import EngagementService
        context = SimpleNamespace(organization={"id": "org", "bot_languages": "English"},
            lead={"id": "lead"}, conversation={"messages": [{"direction": "inbound", "body": "Hello"}]})
        decision = EngagementDecision(should_engage=True, message="Hello!", file_document_id=None,
            crm_actions=[], qualification_updates=[], reason="NORMAL_CONVERSATION", model="test")
        def generate(self, **kwargs):
            self._build_input(context=context)
            return decision
        with patch.object(runtime, "_INSTALLED", False), \
             patch.object(EngagementService, "engage", generate), \
             patch.object(EngagementService, "_build_input", return_value="{}"):
            runtime.install_final_reply_language()
            service = EngagementService(provider=Mock(), context_builder=Mock())
            self.assertIs(service.engage(organization=SimpleNamespace(id="org"), lead=SimpleNamespace(id="lead")), decision)
            service.context_builder.build.assert_not_called()
        self.assertIsNone(runtime._TURN_CONTEXT.get())

    def test_sandbox_does_not_promise_real_team_contact(self):
        from apps.ai_engagement.services.playground_finalization import enforce_preview_action_honesty
        decision = EngagementDecision(should_engage=True,
            message="Thank you. I will pass your request to our team and they will contact you soon.",
            file_document_id=None, crm_actions=[], qualification_updates=[],
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="test")
        result = enforce_preview_action_honesty(decision=decision, events=[{"type": "stage_transition", "status": "preview"}],
                                               files=[], requested_text="Please call me.", allowed_languages=["English"])
        self.assertNotIn("will contact", result.message)
        self.assertNotIn("will pass", result.message)
        self.assertIn("no live call", result.message)

    def test_first_turn_volunteered_answers_keep_welcome_file_review(self):
        from apps.ai_engagement.graph.workflow import _welcome_due_for_context
        context = SimpleNamespace(conversation={"channel": "sandbox", "message_count": 1,
            "messages": [{"direction": "inbound", "body": "My problem is slow replies. I receive 20 leads daily."}]})
        decision = SimpleNamespace(should_engage=True, reason_code="ANSWER_ORG_QUESTION",
                                   qualification_updates=[{"requirement_id": "problem"}])
        self.assertTrue(_welcome_due_for_context(decision=decision, context=context, lead=SimpleNamespace()))
        context.conversation["messages"][0]["body"] += " What is the price?"
        self.assertFalse(_welcome_due_for_context(decision=decision, context=context, lead=SimpleNamespace()))

    def test_explicit_request_and_short_answers_preserve_language(self):
        messages = [{"direction": "inbound", "body": "Please reply in German. What does DIY cost?"},
                    {"direction": "inbound", "body": "Slow replies"}]
        self.assertEqual(requested_language(configured="English, German", messages=messages), "German")
        self.assertEqual(requested_language(configured="English", messages=messages), "English")

    def test_translation_preserves_effects_and_price(self):
        decision = EngagementDecision(should_engage=True, message="DIY costs ₹2,999 per month per user.",
            file_document_id=18, crm_actions=[{"type": "test_preview"}], qualification_updates=[],
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="test")
        service = SimpleNamespace(provider=Mock(), _generate_provider_text=Mock(return_value=SimpleNamespace(
            text=json.dumps({"message": "Der DIY-Plan kostet ₹2,999 pro Monat und Benutzer."}))))
        service._generate_provider_text.side_effect = [service._generate_provider_text.return_value,
                                                      SimpleNamespace(text='{"faithful":true}')]
        context = SimpleNamespace(organization={"bot_languages": "English, German"},
            conversation={"messages": [{"direction": "inbound", "body": "Bitte antworten Sie auf Deutsch. Was kostet der DIY-Plan pro Monat?"}]})
        result = finalize_reply_language(service=service, decision=decision, context=context,
                                        organization=SimpleNamespace(id=1), lead=SimpleNamespace(id=2))
        self.assertIn("kostet", result.message)
        self.assertEqual(result.crm_actions, decision.crm_actions)
        self.assertEqual(result.file_document_id, 18)
        service._generate_provider_text.side_effect = [SimpleNamespace(text='{"message":"Der Preis ist ₹2,999."}'),
                                                      SimpleNamespace(text='{"faithful":false}')]
        with self.assertRaises(EngagementError):
            finalize_reply_language(service=service, decision=decision, context=context,
                                    organization=SimpleNamespace(id=1), lead=SimpleNamespace(id=2))
        service._generate_provider_text.side_effect = None
        service._generate_provider_text.return_value.text = '{"message":"Der Preis ist ₹9,999."}'
        with self.assertRaises(EngagementError):
            finalize_reply_language(service=service, decision=decision, context=context,
                                    organization=SimpleNamespace(id=1), lead=SimpleNamespace(id=2))

    def test_live_reproductions_are_not_qualification_evidence(self):
        cases = (
            ("Bitte antworten Sie auf Deutsch. Was kostet der DIY-Plan pro Monat?", "DIY-Plan"),
            ("Bitte antworten Sie auf Deutsch. Was kostet der DIY-Plan pro Monat?", "Bitte antworten Sie auf Deutsch"),
            ("ਤੁਹਾਡੇ DIY ਪਲਾਨ ਦੀ ਕੀਮਤ ਕਿੰਨੀ ਹੈ? ਕਿਰਪਾ ਕਰਕੇ ਪੰਜਾਬੀ ਵਿੱਚ ਜਵਾਬ ਦਿਓ।", "DIY ਪਲਾਨ"),
            ("ਤੁਹਾਡੇ DIY ਪਲਾਨ ਦੀ ਕੀਮਤ ਕਿੰਨੀ ਹੈ? ਕਿਰਪਾ ਕਰਕੇ ਪੰਜਾਬੀ ਵਿੱਚ ਜਵਾਬ ਦਿਓ।", "ਪੰਜਾਬੀ ਵਿੱਚ ਜਵਾਬ ਦਿਓ"),
        )
        for source, evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertTrue(_non_answer_evidence(evidence, source))
        self.assertFalse(_non_answer_evidence("20 leads per day", "I receive 20 leads per day, what is the DIY price?"))
