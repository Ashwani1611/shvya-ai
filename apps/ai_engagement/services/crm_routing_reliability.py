from __future__ import annotations

import re
from typing import Any

from django.utils import timezone


_INSTALLED = False
_CALL_REQUEST_RE = re.compile(
    r"\b(?:call\s+me|please\s+call|give\s+me\s+a\s+call|connect\s+with\s+me|speak\s+with\s+me)\b",
    flags=re.IGNORECASE,
)
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
    "qualified": "qualification",
    "qualifies": "qualification",
    "qualifying": "qualification",
    "completed": "complete",
    "completion": "complete",
    "passes": "pass",
    "passed": "pass",
    "answered": "answer",
    "questions": "question",
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


def _confirmation_context(context, latest_text: str) -> str:
    """Resolve a short confirmation against only the immediately prior reply.

    This permits a natural WhatsApp exchange such as "Would you like a human to
    call?" -> "Yes" without treating older conversation text as fresh routing
    authority. Negative or ambiguous short replies never use this path.
    """

    normalized = _clean(latest_text).casefold().strip(" .,!?:;'\"")
    if normalized not in {
        "yes",
        "yes please",
        "correct",
        "that's right",
        "thats right",
        "sure",
        "okay",
        "ok",
    }:
        return ""

    conversation = getattr(context, "conversation", None)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    latest_index = None
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if isinstance(item, dict) and item.get("direction") == "inbound" and _clean(item.get("body")):
            latest_index = index
            break
    if latest_index is None:
        return ""
    for index in range(latest_index - 1, -1, -1):
        item = messages[index]
        if not isinstance(item, dict) or not _clean(item.get("body")):
            continue
        if item.get("direction") != "outbound":
            return ""
        return f"{_clean(item.get('body'))} {latest_text}"
    return ""


def _ensure_datetime_reminder(controlled, latest_text):
    """Create deterministic normal-conversation reminders without qualification logic."""
    if any(item.get("type") == "create_reminder" for item in controlled):
        return
    from apps.ai_engagement.services.qualification_crm_action_runtime import (
        _parse_grounded_due_at,
    )

    due_at = _parse_grounded_due_at(latest_text)
    if due_at:
        description = "Lead provided a specific date/time for follow-up."
    elif _CALL_REQUEST_RE.search(str(latest_text or "")):
        due_at = timezone.now().isoformat()
        description = "Lead explicitly requested a call or direct connection."
    else:
        return

    controlled.append(
        {
            "type": "create_reminder",
            "title": "Follow up with lead",
            "description": description,
            "due_at": due_at,
        }
    )


def _available_stage_context(context):
    pipeline = getattr(context, "pipeline", None)
    stage = getattr(context, "stage", None)
    if not isinstance(pipeline, dict):
        return {}, ""
    allowed = {
        str(item.get("id")): item
        for item in pipeline.get("available_stages") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    current_stage_id = str(stage.get("id") or "") if isinstance(stage, dict) else ""
    return allowed, current_stage_id


def _looks_like_qualification_completion(value: Any) -> bool:
    """Identify completion semantics without depending on any stage display name."""
    tokens = _tokens(value)
    has_qualification = "qualification" in tokens
    has_terminal_signal = bool(
        tokens & {"complete", "pass", "answer", "required"}
    )
    return has_qualification and has_terminal_signal


def _is_qualification_completion_destination(
    *,
    destination,
    runtime_policy,
    qualification_state,
) -> bool:
    """Reserve qualification-completion destinations for the backend contract.

    Legacy state may still expose a historical completion-stage id. It is used
    only as a deny-list hint here, never as execution authority. The generic
    boundary also recognizes authored completion semantics in stage descriptions
    and Stage Shifting rules, so no particular stage name is hardcoded.
    """
    stage_id = str(destination.get("id") or "").strip()
    reserved_ids = {
        str((qualification_state or {}).get(key) or "").strip()
        for key in ("qualified_stage_id", "completion_stage_id")
        if str((qualification_state or {}).get(key) or "").strip()
    }
    if stage_id and stage_id in reserved_ids:
        return True

    description = _clean(destination.get("description"))
    if description and _looks_like_qualification_completion(description):
        return True

    from apps.ai_engagement.services.engagement_instruction_runtime import (
        _stage_rule_references_destination,
    )

    rules = ((runtime_policy or {}).get("crm") or {}).get("stage_shifting") or []
    for rule in rules:
        if not isinstance(rule, str):
            continue
        if not _stage_rule_references_destination(rule, destination):
            continue
        if _looks_like_qualification_completion(rule):
            return True
    return False


def _stage_action_supported(
    *,
    action,
    context,
    runtime_policy,
    qualification_state,
    latest_text: str,
) -> bool:
    if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
        return False
    shift = action.get("stage_shift")
    stage_id = (
        str(shift.get("stage_id") or "").strip()
        if isinstance(shift, dict)
        else ""
    )
    allowed, current_stage_id = _available_stage_context(context)
    destination = allowed.get(stage_id)
    if not stage_id or destination is None or stage_id == current_stage_id:
        return False

    if _is_qualification_completion_destination(
        destination=destination,
        runtime_policy=runtime_policy,
        qualification_state=qualification_state,
    ):
        return False

    from apps.ai_engagement.services.engagement_instruction_runtime import (
        _condition_part,
        _stage_rule_references_destination,
        _strong_evidence_match,
        _condition_evidence_match,
    )

    evidence_texts = [latest_text]
    confirmation = _confirmation_context(context, latest_text)
    if confirmation:
        evidence_texts.append(confirmation)

    rules = ((runtime_policy or {}).get("crm") or {}).get("stage_shifting") or []
    authored_destination = False
    for rule in rules:
        if not isinstance(rule, str) or not _stage_rule_references_destination(rule, destination):
            continue
        authored_destination = True
        condition = _condition_part(rule, destination)
        if condition and any(
            _condition_evidence_match(text, condition, context) for text in evidence_texts
        ):
            return True

    if authored_destination:
        return False

    description = _clean(destination.get("description"))
    pipeline_description = _clean(destination.get("pipeline_description"))
    for authored in (description, pipeline_description):
        if authored and any(
            _strong_evidence_match(text, authored) for text in evidence_texts
        ):
            return True

    latest_tokens = _tokens(latest_text)
    destination_tokens = _tokens(destination.get("name"))
    return bool(destination_tokens and destination_tokens.issubset(latest_tokens))


def _sanitize_existing_stage_actions(
    controlled,
    *,
    context,
    runtime_policy,
    qualification_state,
    latest_text,
):
    """Remove legacy/model stage proposals lacking current customer/config evidence."""
    sanitized = []
    for action in controlled or []:
        if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
            sanitized.append(action)
            continue
        if _stage_action_supported(
            action=action,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            latest_text=latest_text,
        ):
            sanitized.append(action)
    return sanitized


def _allow_explicit_model_stage_move(
    controlled,
    *,
    decision,
    context,
    runtime_policy,
    qualification_state,
    latest_text,
):
    """Preserve only evidence-backed ordinary stage routing.

    Qualification-completion stages are not owned by this policy builder. They
    are constructed later by the deterministic qualification execution contract.
    """
    if any(item.get("type") == "pipeline_transition" for item in controlled):
        return
    for action in getattr(decision, "crm_actions", []) or []:
        if _stage_action_supported(
            action=action,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            latest_text=latest_text,
        ):
            shift = action.get("stage_shift") or {}
            controlled.append(
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": str(shift.get("stage_id") or "")},
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
        controlled = _sanitize_existing_stage_actions(
            controlled,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            latest_text=latest_text,
        )
        _allow_explicit_model_stage_move(
            controlled,
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
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
        "- If the lead provides a concrete follow-up date/time or explicitly asks to be called, the backend may create a reminder.\n"
        "- Qualification attributes, qualification completion, and qualification completion-stage routing are backend-owned and MUST NOT be inferred here."
    )
    if extra not in EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS:
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
            f"{EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS}\n\n{extra}"
        )

    _INSTALLED = True
