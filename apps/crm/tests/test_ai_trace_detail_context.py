from django.test import SimpleTestCase

from apps.crm.views.ai_trace import _trace_detail_context


class AITraceDetailContextTests(SimpleTestCase):
    def test_phase3_to_7_sections_are_exposed_to_web_detail(self):
        details = {
            "policy": {"outcome": "ANSWER_THEN_QUALIFY"},
            "grounding": {"verified": True},
            "memory": {"facts": {"lead_volume": 30}},
            "action_plan": {"actions": [{"action_type": "HUMAN_HANDOFF"}]},
        }

        context = _trace_detail_context(details)

        self.assertEqual(context["policy"], details["policy"])
        self.assertEqual(context["grounding"], details["grounding"])
        self.assertEqual(context["memory"], details["memory"])
        self.assertEqual(context["action_plan"], details["action_plan"])
        self.assertEqual(context["intent"], {"status": "NOT_AVAILABLE"})

    def test_non_mapping_details_fail_closed_to_empty_sections(self):
        context = _trace_detail_context(None)

        self.assertEqual(context["policy"], {})
        self.assertEqual(context["grounding"], {})
        self.assertEqual(context["memory"], {})
        self.assertEqual(context["action_plan"], {})
