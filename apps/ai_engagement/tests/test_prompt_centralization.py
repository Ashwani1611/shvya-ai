from pathlib import Path

from apps.ai_engagement.prompts import (
    BUMP_UP_MESSAGE_INSTRUCTIONS,
    INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS,
    QUALIFICATION_SUMMARY_INSTRUCTIONS,
)
from apps.ai_engagement.services.internal_summary import InternalSummaryService
from apps.ai_engagement.services.qualification import QualificationService


def test_internal_summary_service_uses_canonical_prompt():
    assert (
        InternalSummaryService.SUMMARY_INSTRUCTIONS
        == INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS
    )
    assert InternalSummaryService.DEFAULT_MESSAGE_LIMIT == 24


def test_qualification_service_uses_canonical_prompt():
    assert (
        QualificationService.QUALIFICATION_INSTRUCTIONS
        == QUALIFICATION_SUMMARY_INSTRUCTIONS
    )


def test_bump_up_task_uses_canonical_prompt_module():
    source = (Path(__file__).resolve().parents[1] / "tasks.py").read_text()
    dispatch_source = source.split(
        '@shared_task(name="ai.dispatch_bump_ups")', 1
    )[1].split(
        "# ============================================================\n# INTERNAL CONVERSATION SUMMARY",
        1,
    )[0]

    assert "BUMP_UP_MESSAGE_INSTRUCTIONS" in dispatch_source
    assert (
        "This is a scheduled bump-up. The lead has not replied for at least one hour."
        not in dispatch_source
    )
    assert BUMP_UP_MESSAGE_INSTRUCTIONS.startswith(
        "This is a scheduled bump-up. The lead has not replied for at least one hour."
    )
