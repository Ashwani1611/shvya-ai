"""Internal briefings use the same score as CRM cards without a scoring call."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from apps.ai_engagement.services.lead_briefing import LeadBriefingService


@pytest.mark.parametrize("score,assessed,priority", [(2, True, False), (8, True, True), (None, False, False)])
def test_model_score_cannot_replace_backend_score_or_priority(score, assessed, priority):
    provider = Mock()
    provider.generate_text.return_value = SimpleNamespace(text=json.dumps({
        "notes": "The lead asked about a demo.", "attributes": {},
        "intent_score": 10, "high_priority_lead": True,
    }), model="test-model")
    builder = Mock()
    builder.build.return_value.as_dict.return_value = {
        "organization": {}, "lead": {}, "attributes": {},
        "conversation_summary": "", "conversation": [],
    }
    lead = SimpleNamespace(id="lead-1")
    with patch("apps.ai_engagement.services.intent_score.compute_intent_score", return_value={
        "score": score, "assessed": assessed,
    }) as compute:
        result = LeadBriefingService(provider=provider, context_builder=builder).generate(
            organization=SimpleNamespace(id="org-1"), lead=lead,
        )
    assert result.intent_score == score
    assert result.high_priority_lead is priority
    compute.assert_called_once_with(lead=lead)
    provider.generate_text.assert_called_once()
    payload = json.loads(provider.generate_text.call_args.kwargs["input_text"])
    assert payload["backend_scoring"] == {"intent_score": score, "high_priority_lead": priority}
