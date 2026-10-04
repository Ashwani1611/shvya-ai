from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement_instruction_runtime import _strong_evidence_match


class StageRuleRecursionTests(SimpleTestCase):
    def test_unsupported_boolean_syntax_never_recurses_or_authorizes_stage_move(self):
        for condition in (
            "call and/or demo", "call or", "or call", "call (or demo)",
            "call and", "and call", "call,or demo", "call or,demo",
            "call or or demo", "call and and demo",
        ):
            with self.subTest(condition=condition):
                self.assertFalse(_strong_evidence_match("call demo", condition))

    def test_supported_alternatives_and_conjunctions_keep_their_meaning(self):
        self.assertTrue(_strong_evidence_match("call", "call or demo"))
        self.assertFalse(_strong_evidence_match("call", "call and payment"))
        self.assertTrue(_strong_evidence_match("call payment", "call and payment"))
        self.assertFalse(_strong_evidence_match("not interested", "call or demo"))

    def test_live_ultra_hot_description_does_not_crash_an_unrelated_pricing_turn(self):
        description = (
            "A highly engaged qualified lead showing strong buying intent, urgency, "
            "or clear readiness to purchase or proceed."
        )
        self.assertFalse(_strong_evidence_match(
            "What plans and pricing does SHVYA offer?", description,
        ))
