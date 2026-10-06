from dataclasses import dataclass

import pytest

from apps.ai_engagement.services.qualification_state import (
    _non_answer_evidence, project_answer_updates,
)
from apps.ai_engagement.services.playground_finalization import enforce_preview_action_honesty


def test_pure_demo_request_does_not_establish_an_unrelated_problem():
    source = "I want a demo call with your team. Please call me."
    requirement = {"id": "problem", "question": "What is your biggest issue managing leads?"}
    with pytest.raises(ValueError, match="valid inbound evidence"):
        project_answer_updates(
            state={}, requirements=[requirement],
            updates=[{"requirement_id": "problem", "value": "No proper tracking",
                      "source_message_id": "m", "evidence": source}],
            messages=[{"id": "m", "direction": "inbound", "body": source}],
        )
    assert _non_answer_evidence("demo call", source, requirement=requirement)
    assert _non_answer_evidence("Please call me", source, requirement=requirement)


def test_assistance_answers_and_volunteered_facts_remain_usable():
    assert not _non_answer_evidence(
        "demo call", "I want a demo call.",
        requirement={"question": "Would you like a demo call?"},
    )
    assert not _non_answer_evidence(
        "slow replies", "I want a demo because my biggest problem is slow replies.",
        requirement={"question": "What is your biggest problem?"},
    )
    assert _non_answer_evidence(
        "call", "I don't want a call.",
        requirement={"question": "Are you running paid ads?"},
    )


@dataclass
class Reply:
    message: str
    should_engage: bool = True
    file_document_id: int | None = None


@pytest.mark.parametrize("promise", [
    "I will coordinate with our team to schedule this for you.",
    "I'll pass it along.",
])
def test_sandbox_does_not_promise_unperformed_coordination(promise):
    output = enforce_preview_action_honesty(
        decision=Reply(promise), events=[], files=[],
        requested_text="Please arrange a demo call.", allowed_languages=["English"],
    )
    assert promise not in output.message
    assert "Sandbox" in output.message
