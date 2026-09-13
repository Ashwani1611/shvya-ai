from __future__ import annotations

import re
from typing import Any


_INSTALLED = False
_NEW_LEAD_STAGE_NAMES = {"new lead", "new leads"}
_TERMINAL_REQUIREMENT_STATES = {"answered", "skipped", "not_applicable"}

_STOP_TOKENS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
    "from", "get", "gets", "how", "i", "in", "is", "it", "lead", "leads",
    "of", "on", "or", "per", "please", "the", "to", "what", "where", "which",
    "who", "with", "you", "your", "currently", "current", "now", "right",
    "answer", "answers", "attribute", "field", "store", "save", "capture", "fill",
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


def _normalize_stage_name(value: Any) -> str:
    value = _clean(value).casefold()
    return "new lead" if value == "new leads" else value


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


def _organization_qualified_stage(lead):
    """Resolve Qualified across the organization, preferring the current pipeline."""
    pipeline = getattr(lead, "pipeline", None)
    if pipeline is not None:
        for stage in pipeline.stages.filter(is_active=True).order_by("display_order", "name", "id"):
            if _normalize_stage_name(stage.name) == "qualified":
                return stage

    organization = getattr(lead, "organization", None)
    if organization is None:
        return None

    pipelines = (
        organization.pipelines.filter(is_active=True)
        .exclude(id=getattr(lead, "pipeline_id", None))
        .prefetch_related("stages")
        .order_by("name", "id")
    )
    for candidate_pipeline in pipelines:
        stages = sorted(
            (stage for stage in candidate_pipeline.stages.all() if stage.is_active),
            key=lambda stage: (stage.display_order, stage.name.casefold(), str(stage.id)),
        )
        for stage in stages:
            if _normalize_stage_name(stage.name) == "qualified":
                return stage
    return None


def _definition_option_values(definition: dict[str, Any]) -> set[str]:
    values = definition.get("options")
    if not isinstance(values, list):
        return set()
    return {_clean(item).casefold() for item in values if _clean(item)}


def _requirement_option_values(requirement: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in requirement.get("options") or []:
        if isinstance(item, dict):
            value = _clean(item.get("value"))
        else:
            value = _clean(item)
        if value:
            result.add(value.casefold())
    return result


def _attribute_score(definition: dict[str, Any], requirement: dict[str, Any]) -> float:
    key = _clean(definition.get("key")).casefold()
    name = _clean(definition.get("name")).casefold()
    description = _clean(definition.get("description")).casefold()
    req_id = _clean(requirement.get("id")).casefold()
    label = _clean(requirement.get("label")).casefold()
    question = _clean(requirement.get("question")).casefold()

    if key and key == req_id:
        return 100.0
    if name and label and name == label:
        return 99.0
    if description and (label and label in description or question and question.split("\n", 1)[0] in description):
        return 98.0

    requirement_tokens = _tokens(f"{label} {question.split(chr(10), 1)[0]}")
    name_tokens = _tokens(f"{key} {name}")
    description_tokens = _tokens(description)

    score = 0.0
    if name_tokens and requirement_tokens:
        overlap = name_tokens & requirement_tokens
        name_coverage = len(overlap) / len(name_tokens)
        requirement_coverage = len(overlap) / len(requirement_tokens)
        if overlap:
            score = max(score, 82.0 + 10.0 * max(name_coverage, requirement_coverage))
        if name_tokens.issubset(requirement_tokens) or requirement_tokens.issubset(name_tokens):
            score = max(score, 94.0)

    if description_tokens and requirement_tokens:
        overlap = description_tokens & requirement_tokens
        requirement_coverage = len(overlap) / len(requirement_tokens)
        if len(overlap) >= 2:
            score = max(score, 88.0 + 8.0 * requirement_coverage)
        elif len(overlap) == 1 and (len(requirement_tokens) <= 3 or requirement_coverage >= 0.34):
            # Attribute descriptions are authored mapping guidance. A single
            # distinctive concept is sufficient only when the qualification
            # requirement itself is compact.
            score = max(score, 84.0)

    req_options = _requirement_option_values(requirement)
    attr_options = _definition_option_values(definition)
    if req_options and attr_options:
        overlap = req_options & attr_options
        if overlap:
            coverage = len(overlap) / min(len(req_options), len(attr_options))
            score = max(score, 94.0 + 5.0 * coverage)

    return score


def _best_attribute_key(*, requirement, definitions, used) -> str | None:
    ranked: list[tuple[float, str]] = []
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        key = _clean(definition.get("key"))
        if not key or key in used:
            continue
        ranked.append((_attribute_score(definition, requirement), key))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked or ranked[0][0] < 82.0:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 3.0 and ranked[0][0] < 96.0:
        return None
    return ranked[0][1]


def _latest_answer_updates(*, qualification_state, latest_message_id):
    if not latest_message_id:
        return []
    updates = []
    for requirement_id, state in (qualification_state.get("requirement_states") or {}).items():
        if not isinstance(state, dict):
            continue
        if _clean(state.get("status")).casefold() != "answered":
            continue
        if str(state.get("source_message_id") or "") != latest_message_id:
            continue
        value = state.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        updates.append({"requirement_id": str(requirement_id), "value": value})
    return updates


def _merge_attribute_updates(controlled, *, qualification_state, requirements, context, latest_message_id):
    definitions = [
        item for item in ((getattr(context, "pipeline", None) or {}).get("attribute_definitions") or [])
        if isinstance(item, dict) and _clean(item.get("key"))
    ]
    if not definitions:
        return
    by_requirement = {
        str(item.get("id") or ""): item
        for item in requirements or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    used = set()
    deterministic = []
    for update in _latest_answer_updates(
        qualification_state=qualification_state,
        latest_message_id=latest_message_id,
    ):
        requirement = by_requirement.get(update["requirement_id"])
        if requirement is None:
            continue
        key = _best_attribute_key(requirement=requirement, definitions=definitions, used=used)
        if not key:
            continue
        used.add(key)
        deterministic.append({"key": key, "value": update["value"]})

    if not deterministic:
        return
    existing = next((item for item in controlled if item.get("type") == "attribute_updates"), None)
    if existing is None:
        controlled.insert(0, {"type": "attribute_updates", "updates": deterministic})
        return
    values = {
        str(item.get("key")): item
        for item in existing.get("updates") or []
        if isinstance(item, dict) and item.get("key")
    }
    for item in deterministic:
        values[item["key"]] = item
    existing["updates"] = list(values.values())


def _all_required_complete(qualification_state, requirements) -> bool:
    if not requirements:
        return False
    states = qualification_state.get("requirement_states") or {}
    for requirement in requirements:
        if not requirement.get("required", True):
            continue
        status = _clean((states.get(str(requirement.get("id") or "")) or {}).get("status")).casefold()
        if status not in _TERMINAL_REQUIREMENT_STATES:
            return False
    return True


def _qualified_stage_id_from_context(context) -> str:
    pipeline = getattr(context, "pipeline", None)
    if not isinstance(pipeline, dict):
        return ""
    candidates = []
    for item in pipeline.get("available_stages") or []:
        if not isinstance(item, dict) or _normalize_stage_name(item.get("name")) != "qualified":
            continue
        candidates.append(item)
    candidates.sort(key=lambda item: (not bool(item.get("is_current_pipeline")), _clean(item.get("pipeline_name")), str(item.get("id") or "")))
    return str(candidates[0].get("id") or "") if candidates else ""


def _ensure_qualified_transition(controlled, *, context, qualification_state, requirements):
    stage = getattr(context, "stage", None)
    stage_name = _normalize_stage_name(stage.get("name") if isinstance(stage, dict) else "")
    if stage_name != "new lead" or not _all_required_complete(qualification_state, requirements):
        return
    stage_id = _clean(qualification_state.get("qualified_stage_id")) or _qualified_stage_id_from_context(context)
    if not stage_id:
        return
    controlled[:] = [item for item in controlled if item.get("type") != "pipeline_transition"]
    controlled.append({"type": "pipeline_transition", "stage_shift": {"stage_id": stage_id}})


def _ensure_datetime_reminder(controlled, latest_text):
    if any(item.get("type") == "create_reminder" for item in controlled):
        return
    from apps.ai_engagement.services.qualification_crm_action_runtime import _parse_grounded_due_at

    due_at = _parse_grounded_due_at(latest_text)
    if not due_at:
        return
    controlled.append({
        "type": "create_reminder",
        "title": "Follow up with lead",
        "description": "Lead provided a specific date/time for follow-up.",
        "due_at": due_at,
    })


def _allow_explicit_model_stage_move(controlled, *, decision, context, latest_text):
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
        stage_id = str(shift.get("stage_id") or "").strip() if isinstance(shift, dict) else ""
        destination = allowed.get(stage_id)
        if not stage_id or destination is None or stage_id == current_stage_id:
            continue
        if _normalize_stage_name(destination.get("name")) == "qualified":
            continue
        description = _clean(destination.get("description"))
        if description:
            controlled.append({"type": "pipeline_transition", "stage_shift": {"stage_id": stage_id}})
            return
        # With no authored stage description, accept only an explicit destination
        # reference in the lead's latest message; never infer a silent rule.
        destination_tokens = _tokens(destination.get("name"))
        if destination_tokens and destination_tokens.issubset(latest_tokens):
            controlled.append({"type": "pipeline_transition", "stage_shift": {"stage_id": stage_id}})
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
        latest_message_id, latest_text = _latest_inbound(context)
        stage = getattr(context, "stage", None)
        stage_name = _normalize_stage_name(stage.get("name") if isinstance(stage, dict) else "")

        if stage_name == "new lead":
            _merge_attribute_updates(
                controlled,
                qualification_state=qualification_state,
                requirements=requirements,
                context=context,
                latest_message_id=latest_message_id,
            )
            _ensure_qualified_transition(
                controlled,
                context=context,
                qualification_state=qualification_state,
                requirements=requirements,
            )

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
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_state as qualification_state_module
    qualification_state_module._qualified_stage = _organization_qualified_stage

    from apps.ai_engagement.graph import policy_actions as policy_actions_module
    policy_actions_module.build_controlled_actions = _wrap_controlled_actions(
        policy_actions_module.build_controlled_actions
    )

    from apps.ai_engagement.services.engagement import EngagementService
    extra = (
        "CRM ROUTING RELIABILITY\n"
        "- Attribute definitions include key, name, description, type and options. "
        "Use the attribute description as mapping guidance whenever the latest lead message supplies that value.\n"
        "- When conversation evidence clearly satisfies an available stage description, propose exactly one pipeline_transition using that stage id. "
        "Cross-pipeline stage ids are allowed when they are present in pipeline.available_stages.\n"
        "- Outside New Lead, do not ask qualification questions; normal conversation and evidence-bound CRM actions may continue.\n"
        "- If the lead provides a concrete follow-up date and time, propose a reminder; the backend will validate the time."
    )
    if extra not in EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS:
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = f"{EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS}\n\n{extra}"

    _INSTALLED = True
