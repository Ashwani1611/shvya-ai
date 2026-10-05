from __future__ import annotations

import json
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.ai_provider import OpenAIProvider
from apps.ai_engagement.services.model_router import (
    FAST_TIER,
    REASONING_TIER,
    STANDARD_TIER,
    classify_tier,
    fallback_models,
    route_model,
)
from apps.ai_engagement.services.turn_health import evaluate_turn_health


class AdaptiveModelRoutingTests(SimpleTestCase):
    def test_simple_greeting_uses_fast_tier(self):
        tier, reason = classify_tier(
            input_text=json.dumps(
                {
                    "recent_conversation": {
                        "messages": [{"direction": "inbound", "body": "Hello"}]
                    }
                }
            ),
            metadata={"task": "engagement", "phase": "primary"},
        )
        self.assertEqual(tier, FAST_TIER)
        self.assertEqual(reason, "simple_ack_or_greeting")

    def test_normal_customer_turn_uses_standard_tier(self):
        tier, _ = classify_tier(
            input_text=json.dumps(
                {
                    "recent_conversation": {
                        "messages": [
                            {
                                "direction": "inbound",
                                "body": "Can you explain how your CRM follow-up works?",
                            }
                        ]
                    }
                }
            ),
            metadata={"task": "engagement", "phase": "primary"},
        )
        self.assertEqual(tier, STANDARD_TIER)

    def test_multi_question_sales_turn_uses_reasoning_tier(self):
        tier, reason = classify_tier(
            input_text=json.dumps(
                {
                    "recent_conversation": {
                        "messages": [
                            {
                                "direction": "inbound",
                                "body": (
                                    "How are you different from the competitor? "
                                    "Which plan should I choose?"
                                ),
                            }
                        ]
                    }
                }
            ),
            metadata={
                "task": "engagement",
                "phase": "primary",
                "prompt_mode": "sales_support",
            },
        )
        self.assertEqual(tier, REASONING_TIER)
        self.assertIn(reason, {"sales_objection_or_comparison", "multiple_questions"})

    def test_large_context_does_not_make_simple_customer_message_complex(self):
        payload = {
            "organization_operating_spec": {"about": "x" * 12000},
            "knowledge": [{"content": "y" * 8000}],
            "recent_conversation": {
                "messages": [{"direction": "inbound", "body": "Thanks"}]
            },
        }
        tier, _ = classify_tier(
            input_text=json.dumps(payload),
            metadata={"task": "engagement"},
        )
        self.assertEqual(tier, FAST_TIER)

    def test_configured_tier_model_is_selected(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_ADAPTIVE_ROUTING_ENABLED": "True",
                "OPENAI_STANDARD_MODEL": "gpt-standard-platform",
            },
            clear=False,
        ):
            route = route_model(
                base_model="gpt-base",
                input_text=json.dumps(
                    {
                        "recent_conversation": {
                            "messages": [
                                {
                                    "direction": "inbound",
                                    "body": "Tell me about your follow-up automation.",
                                }
                            ]
                        }
                    }
                ),
                metadata={"task": "engagement"},
            )
        self.assertEqual(route.tier, STANDARD_TIER)
        self.assertEqual(route.model, "gpt-standard-platform")

    def test_superadmin_organization_override_bypasses_adaptive_router(self):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider._explicit_model = False
        provider.model = "gpt-platform-default"

        with patch.dict(
            "os.environ",
            {
                "OPENAI_STANDARD_MODEL": "gpt-platform-standard",
                "OPENAI_REASONING_MODEL": "gpt-platform-reasoning",
            },
            clear=False,
        ):
            model, tier, reason = provider._model_for_request(
                metadata={
                    "task": "engagement",
                    "model_override": "gpt-superadmin-selected",
                },
                input_text="Why should I choose you instead of your competitor?",
            )

        self.assertEqual(model, "gpt-superadmin-selected")
        self.assertEqual(tier, "fixed")
        self.assertEqual(reason, "superadmin_override")

    def test_internal_routing_metadata_is_not_sent_to_provider(self):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        sanitized = provider._provider_metadata(
            {
                "organization_id": "org",
                "task": "engagement",
                "__routing_model": "internal-model",
                "__fallback_attempted": "1",
            }
        )
        self.assertEqual(
            sanitized,
            {"organization_id": "org", "task": "engagement"},
        )

    def test_reasoning_fallback_order_prefers_operator_fallback_then_standard(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_FALLBACK_MODEL": "gpt-emergency",
                "OPENAI_STANDARD_MODEL": "gpt-standard",
                "OPENAI_FAST_MODEL": "gpt-fast",
            },
            clear=False,
        ):
            result = fallback_models(
                primary_model="gpt-reasoning",
                base_model="gpt-base",
                tier=REASONING_TIER,
            )
        self.assertEqual(
            result,
            ("gpt-emergency", "gpt-standard", "gpt-fast", "gpt-base"),
        )


class TurnHealthTests(SimpleTestCase):
    def test_healthy_turn_scores_high(self):
        result = evaluate_turn_health(
            {
                "status": "completed",
                "grounding": {"approved": True},
                "provider": {"provider_success": True},
            },
            total_ms=1800,
        )
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["band"], "excellent")

    def test_fallback_and_rejected_action_are_visible(self):
        result = evaluate_turn_health(
            {
                "status": "completed",
                "provider": {"fallback_used": True},
                "crm_actions": {
                    "attempts": [{"accepted": [], "rejected": [{"type": "create_reminder"}]}]
                },
            },
            total_ms=9000,
        )
        self.assertLess(result["score"], 90)
        self.assertIn("provider_fallback_used", result["flags"])
        self.assertIn("crm_action_rejected", result["flags"])
        self.assertIn("slow_turn", result["flags"])
