import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.tenant_guard import TenantScopeError

from django.test import SimpleTestCase

from apps.ai_engagement.services.playground_graph_recovery import (
    _grounded_knowledge_message,
    _knowledge_chunks_for_recovery,
)


class PlaygroundConnectedKnowledgeRecoveryTests(SimpleTestCase):
    def test_pricing_question_uses_retrieved_connected_knowledge(self):
        chunks = [
            {
                "content": (
                    "Pricing and plans: Starter plan is ₹999/month. "
                    "Pro plan is ₹2,499/month and includes advanced automation."
                ),
                "similarity": 0.61,
            }
        ]

        message = _grounded_knowledge_message(
            latest_text="price of plan",
            chunks=chunks,
        )

        self.assertIn("₹999/month", message)
        self.assertIn("₹2,499/month", message)

    def test_pre_threshold_retrieval_is_available_to_sandbox_recovery(self):
        low_similarity_chunk = {
            "content": "Starter plan costs ₹999/month.",
            "similarity": 0.31,
        }
        state = {
            "context": SimpleNamespace(knowledge=[]),
            "service": SimpleNamespace(
                context_builder=SimpleNamespace(
                    last_knowledge=[low_similarity_chunk],
                )
            ),
        }

        chunks = _knowledge_chunks_for_recovery(state)
        message = _grounded_knowledge_message(
            latest_text="what are your pricing plans?",
            chunks=chunks,
        )

        self.assertEqual(len(chunks), 1)
        self.assertIn("₹999/month", message)

    def test_feature_question_uses_connected_knowledge_instead_of_qualification(self):
        chunks = [
            {
                "content": (
                    "Shvya features include WhatsApp AI engagement, automated "
                    "follow-ups, lead qualification, CRM tracking, and workflows."
                ),
                "similarity": 0.72,
            }
        ]

        message = _grounded_knowledge_message(
            latest_text="what is shvya and its features",
            chunks=chunks,
        )

        self.assertIn("WhatsApp AI engagement", message)
        self.assertIn("lead qualification", message)

    def test_pricing_prefers_concrete_prices_over_website_navigation(self):
        chunks = [
            {
                "document_name": "Company website",
                "content": (
                    "SHVYA AI Features\nLead capture & qualification\nPricing\nLogin\n"
                    "Start free\nProduct Suite\nUse Cases"
                ),
                "similarity": 0.82,
            },
            {
                "document_name": "Pricing and plans",
                "content": (
                    "Starter plan is ₹999/month.\n"
                    "Pro plan is ₹2,499/month.\n"
                    "Enterprise uses custom pricing."
                ),
                "similarity": 0.58,
            },
        ]

        message = _grounded_knowledge_message(
            latest_text="give me price",
            chunks=chunks,
        )

        self.assertIn("₹999/month", message)
        self.assertIn("₹2,499/month", message)
        self.assertNotIn("Login", message)
        self.assertNotIn("Product Suite", message)

    def test_generic_pricing_navigation_is_not_returned_as_an_answer(self):
        chunks = [
            {
                "document_name": "Company website",
                "content": "Features\nUse Cases\nPricing\nLogin\nCreate account",
                "similarity": 0.91,
            }
        ]

        message = _grounded_knowledge_message(
            latest_text="and its pricing",
            chunks=chunks,
        )

        self.assertEqual(message, "")

    def test_recovery_preserves_knowledge_line_boundaries(self):
        state = {
            "context": SimpleNamespace(
                knowledge=[
                    {
                        "content": (
                            "SHVYA AI Features\n"
                            "Lead capture & qualification\n"
                            "AI follow-up\n"
                            "WhatsApp CRM"
                        ),
                        "similarity": 0.75,
                    }
                ]
            ),
            "service": SimpleNamespace(
                context_builder=SimpleNamespace(last_knowledge=[]),
            ),
        }

        chunks = _knowledge_chunks_for_recovery(state)
        message = _grounded_knowledge_message(
            latest_text="what is shvya and its features",
            chunks=chunks,
        )

        self.assertIn("\n", chunks[0]["content"])
        self.assertIn("Lead capture & qualification", message)
        self.assertIn("WhatsApp CRM", message)
        self.assertLess(len(message), 500)


class SandboxBrainRecoveryTests(SimpleTestCase):
    def state(self):
        provider = Mock()
        provider.generate_text.return_value = SimpleNamespace(text=json.dumps({"message": "Starter is ₹1999 monthly."}), model="org-model")
        context = AIContext(organization={"id": "org-a", "about": "We automate sales.",
            "ai_playbook": "Explain starter pricing.", "bot_languages": "English",
            "sales_support_model": "gpt-5-mini"}, lead={}, pipeline={}, stage={"name": "Qualified"},
            contacts=[], attributes=[], conversation={}, conversation_summary=None,
            qualification_notes=[], knowledge=[])
        return {"organization": SimpleNamespace(id="org-a"),
            "lead": SimpleNamespace(id="playground:session", organization_id="org-a"),
            "context": context, "service": SimpleNamespace(provider=provider),
            "latest_text": "How much is starter?"}

    def test_recovery_reads_all_authored_brain_fields_and_organization_model(self):
        from apps.ai_engagement.services.playground_graph_recovery import _ai_brain_recovery
        state = self.state()
        faqs = [{"source_id": "faq:1", "content": "Starter is ₹1999 monthly."}]
        with patch("apps.ai_engagement.services.authored_knowledge.authored_answer_candidates", return_value=faqs):
            result = _ai_brain_recovery(state)
        call = state["service"].provider.generate_text.call_args.kwargs
        payload = json.loads(call["input_text"])
        self.assertEqual(payload["organization_facts"], "We automate sales.")
        self.assertEqual(payload["ai_playbook"], "Explain starter pricing.")
        self.assertEqual(payload["authored_faq_candidates"], faqs)
        self.assertEqual(call["metadata"]["model_override"], "gpt-5-mini")
        self.assertEqual(result["context"].organization["_authored_faq_candidates"], faqs)
        self.assertIn("₹1999", result["decision"].message)
        self.assertEqual(result["decision"].crm_actions, [])
        self.assertEqual(result["decision"].qualification_updates, [])
        self.assertIsNone(result["decision"].file_document_id)

    def test_recovery_never_reads_another_organization(self):
        from apps.ai_engagement.services.playground_graph_recovery import _ai_brain_recovery
        state = self.state()
        state["context"].organization["id"] = "other-org"
        with patch("apps.ai_engagement.services.authored_knowledge.authored_answer_candidates") as faqs:
            with self.assertRaises(TenantScopeError):
                _ai_brain_recovery(state)
        faqs.assert_not_called()
        state["service"].provider.generate_text.assert_not_called()

    def test_old_technical_replies_are_removed_from_sandbox_history(self):
        from apps.ai_engagement.services.playground import PlaygroundService
        from apps.ai_engagement.services.response_fallbacks import fallback_message
        history = [{"role": "user", "content": "What do you do?"},
                   {"role": "assistant", "content": "We automate sales."}]
        for language in ("English", "Hindi", "Hinglish"):
            history.append({"role": "assistant", "content": fallback_message(kind="technical", bot_languages=language)})
        result = PlaygroundService()._normalize_role_history(history)
        self.assertEqual(result, history[:2])

    def test_failed_sandbox_fallback_raises_test_error_instead_of_fake_reply(self):
        from apps.ai_engagement.services.playground import PlaygroundService, PlaygroundError
        with patch("apps.ai_engagement.services.playground.build_deterministic_fallback_decision", side_effect=RuntimeError("test failure")):
            with self.assertRaisesMessage(PlaygroundError, "No test reply was saved"):
                PlaygroundService()._fallback_decision(organization=SimpleNamespace(id="org-a"),
                    visitor=SimpleNamespace(), message="Price?", cause=RuntimeError("provider failure"))
