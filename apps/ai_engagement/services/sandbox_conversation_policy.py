"""Bind shared conversation strategy to an in-memory Sandbox answer turn."""
from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyContext, ConversationPolicyEngine, ConversationPolicyOutcome,
)
from apps.ai_engagement.services.conversation_policy_runtime import (
    _accepted_result, _capabilities, short_qualification_answer,
)
from apps.ai_engagement.services.intent_types import Intent
from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION, NEW_LEAD_STAGE, apply_unambiguous_reply,
    normalize_stage_name, state_for_lead,
)


def sandbox_policy(*, organization, lead, intent, requirements, message,
                   source_message_id, channel="sandbox"):
    # This adapter cannot write a real lead or bind another session's source.
    lead_id = str(getattr(lead, "pk", "") or "")
    source_id = str(source_message_id or "")
    if (
        hasattr(lead, "_meta") or not lead_id.startswith("playground:")
        or not callable(getattr(lead, "_persist_qualification_state", None))
        or not source_id.startswith(lead_id + ":turn:")
    ):
        return None
    state = state_for_lead(lead, requirements=requirements)
    intents = {intent.primary_intent, *intent.secondary_intents}
    if (
        normalize_stage_name(getattr(getattr(lead, "stage", None), "name", "")) == NEW_LEAD_STAGE
        and Intent.OPT_OUT not in intents
        and short_qualification_answer(intent=intent, text=message)
    ):
        state = apply_unambiguous_reply(
            lead=lead, requirements=requirements, text=message,
            source_message_id=source_id,
        )["state"]
    settings = organization.settings if isinstance(organization.settings, dict) else {}
    qualification_settings = settings.get("ai_qualification") or {}
    continue_now = (
        isinstance(qualification_settings, dict)
        and qualification_settings.get("continue_after_answer") is True
    ) or short_qualification_answer(intent=intent, text=message)
    active = state.get("engagement_mode") == MODE_QUALIFICATION
    decision = ConversationPolicyEngine().decide(ConversationPolicyContext(
        intent_decision=intent, organization_id=str(organization.pk),
        lead_id=lead_id, pipeline_id=str(lead.pipeline_id) if lead.pipeline_id else None,
        stage_id=str(lead.stage_id) if lead.stage_id else None,
        qualification_state=state,
        qualification_result=_accepted_result(state=state, source_message_id=source_id),
        next_requirement_id=state.get("next_requirement_id") if active else None,
        extracted_facts=tuple(intent.facts), ai_allowed=True,
        capabilities=_capabilities(organization), continue_after_answer=continue_now,
        channel=channel,
    ))

    # An unclassified turn may still contain volunteered, source-backed facts.
    # Let the graph's bounded capture review resolve those before choosing a
    # question; a CLARIFY policy bound before that review would reject its draft.
    if decision.outcome == ConversationPolicyOutcome.CLARIFY:
        return None
    return decision
