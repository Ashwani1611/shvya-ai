from types import SimpleNamespace

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
