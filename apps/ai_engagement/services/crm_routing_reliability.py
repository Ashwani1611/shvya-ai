from __future__ import annotations

import re
from typing import Any


_INSTALLED = False
_STOP_TOKENS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "do",
    "does",
    "for",
    "from",
    "get",
    "gets",
    "how",
    "i",
    "in",
    "is",
    "it",
    "lead",
    "leads",
    "of",
    "on",
    "or",
    "per",
    "please",
    "the",
    "to",
    "what",
    "where",
    "which",
    "who",
    "with",
    "you",
    "your",
    "currently",
    "current",
    "now",
    "right",
}
_TOKEN_ALIASES = {
    "ads": "ad",
    "advertising": "ad",
    "advertisement": "ad",
    "advertisements": "ad",
    "daily": "day",
    "days": "day",
    "management": "manage",
    "managing": "manage",
    "managed": "manage",
    "sources": "source",
    "origins": "source",
    "origin": "source",
    "received": "receive",
    "receives": "receive",
    "receiving": "receive",
    "replies": "reply",
    "responses": "response",
    "challenges": "challenge",
    "problems": "problem",
    "issues": "issue",
    "referrals": "referral",
    "walkins": "walkin",
    "sheets": "sheet",
    "humans": "human",
    "person": "human",
    "people": "human",
    "agents": "agent",
    "meetings": "meeting",
    "calls": "call",
    "callbacks": "callback",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _tokens(value: Any) -> set[str]:
    result: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", _clean(value).casefold()):
        token = _TOKEN_ALIASES.get(raw, raw)
        if token and token not in _STOP_TOKENS:
            result.add(token)
    return result


def _latest_inbound(context) -> tuple[str, str]:
    conversation = getattr(context, "conversation", None)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("direction") != "inbound":
            continue
        body = _clean(message.get("body"))
        if body:
            return str(message.get("id") or "").strip(), body
    return "", ""


def _ensure_datetime_reminder(controlled, latest_text):
    """Create a deterministic reminder from a concrete customer date/time."""
    if any(item.get("type") == "create_reminder" for item in controlled):
        return
    from apps.ai_engagement.services.qualification_crm_action_runtime import (
        _parse_grounded_due_at,
    )

    due_at = _parse_grounded_due_at(latest_text)
    if not due_at:
        return
    controlled.append(
        {
            "type": "create_reminder",
            "title": "Follow up with lead",
            "description": "Lead provided a specific date/time for follow-up.",
            "due_at": due_at,
        }
    )


def _allow_explicit_model_stage_move(controlled, *, decision, context, latest_text):
    """Preserve ordinary evidence-bound stage routing outside qualification ownership.

    Qualification-completion stages are built by the backend execution contract.
    This function only considers explicit model-proposed stage actions and leaves
    the final tenant/evidence gate to ``stage_transition_evidence``.
    """
    if any(item.get("type") == "pipeline_transition" for item in controlled):
        return
    pipeline = getattr(context, "pipeline", None)
    stage = getattr(context, "stage", None)
    if not isinstance(pipeline, dict):
        return
    current_stage_id = str(stage.get("id") or "") if isinstance(stage, dict) else ""
    allowed = {
        str(item.get("id")): item
        for item in pipeline.get("available_stages") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    latest_tokens = _tokens(latest_text)

    for action in getattr(decision, "crm_actions", []) or []:
        if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
            continue
        shift = action.get("stage_shift")
        stage_id = (
            str(shift.get("stage_id") or "").strip()
            if isinstance(shift, dict)
            else ""
        )
        destination = allowed.get(stage_id)
        if not stage_id or destination is None or stage_id == current_stage_id:
            continue

        description = _clean(destination.get("description"))
        destination_tokens = _tokens(destination.get("name"))
        # A described stage can be proposed and is still checked by the executor
        # evidence gate. Without a description, require the customer to name the
        # destination explicitly before allowing the proposal into controlled
        # actions.
        if description or (
            destination_tokens and destination_tokens.issubset(latest_tokens)
        ):
            controlled.append(
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": stage_id},
                }
            )
            return


def _wrap_controlled_actions(current_builder):
    def build(*, decision, context, runtime_policy, qualification_state, requirements):
        controlled, result = current_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        controlled = [dict(item) for item in controlled]
        _latest_message_id, latest_text = _latest_inbound(context)

        _allow_explicit_model_stage_move(
            controlled,
            decision=decision,
            context=context,
            latest_text=latest_text,
        )
        _ensure_datetime_reminder(controlled, latest_text)
        return controlled, result

    return build


def install_crm_routing_reliability() -> None:
    """Install only non-qualification CRM reliability behavior.

    This module intentionally no longer performs fuzzy qualification->attribute
    mapping and no longer invents/chooses a qualification completion stage.
    Those responsibilities belong exclusively to the qualification execution
    contract and explicit organization configuration.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    policy_actions_module.build_controlled_actions = _wrap_controlled_actions(
        policy_actions_module.build_controlled_actions
    )

    from apps.ai_engagement.services.engagement import EngagementService

    extra = (
        "CRM ROUTING RELIABILITY\n"
        "- For normal conversation, when customer evidence clearly supports an available CRM stage, you may propose exactly one pipeline_transition using that available stage id. Backend validation remains authoritative.\n"
        "- If the lead provides a concrete follow-up date and time, you may propose a reminder; the backend validates and executes it.\n"
        "- Qualification attributes, qualification completion, and qualification completion-stage routing are backend-owned and MUST NOT be inferred here."
    )
    if extra not in EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS:
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
            f"{EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS}\n\n{extra}"
        )

    _INSTALLED = True
