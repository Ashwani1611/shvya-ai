from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


_INSTALLED = False

_GENERIC_RULE_TOKENS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "if",
    "in", "into", "is", "it", "lead", "leads", "move", "moves", "moving",
    "pipeline", "rule", "shift", "shifting", "stage", "stages", "the", "then",
    "this", "to", "user", "when", "whenever", "with",
}
_TOKEN_ALIASES = {
    "people": "human",
    "person": "human",
    "humans": "human",
    "agent": "human",
    "agents": "human",
    "representative": "human",
    "representatives": "human",
    "speak": "talk",
    "speaking": "talk",
    "talking": "talk",
    "calls": "call",
    "callbacks": "callback",
    "meetings": "meeting",
    "photos": "image",
    "photo": "image",
    "pictures": "image",
    "picture": "image",
    "screenshots": "screenshot",
    "payments": "payment",
    "paid": "payment",
    "sellers": "seller",
    "selling": "seller",
    "buyers": "buyer",
    "buying": "buyer",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized(value: Any) -> str:
    return _clean(value).casefold()


def _tokens(value: Any) -> set[str]:
    result: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", _normalized(value)):
        token = _TOKEN_ALIASES.get(raw, raw)
        if token and token not in _GENERIC_RULE_TOKENS:
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


def _crm_policy(runtime_policy: dict[str, Any]) -> dict[str, Any]:
    value = runtime_policy.get("crm") if isinstance(runtime_policy, dict) else {}
    return value if isinstance(value, dict) else {}


def _policy_lines(runtime_policy: dict[str, Any], key: str) -> list[str]:
    value = _crm_policy(runtime_policy).get(key)
    if not isinstance(value, list):
        return []
    return [_clean(item) for item in value if _clean(item)]


def _profile_wrapper(original):
    def profile_from_values(
        *,
        organization_name: str,
        about: str,
        bot_languages: str,
        qualification_requirements: str,
        engagement_instructions: str,
    ):
        from apps.ai_engagement.services.engagement_instruction_policy import (
            compile_engagement_instruction_policy,
            effective_qualification_source,
        )

        effective_raw, source = effective_qualification_source(
            qualification_requirements=qualification_requirements,
            engagement_instructions=engagement_instructions,
        )
        profile = original(
            organization_name=organization_name,
            about=about,
            bot_languages=bot_languages,
            qualification_requirements=effective_raw,
            engagement_instructions=engagement_instructions,
        )
        qualification = profile.setdefault("qualification", {})
        qualification["source"] = source
        profile["engagement_policy"] = compile_engagement_instruction_policy(
            engagement_instructions
        )
        return profile

    return profile_from_values


def _runtime_policy_wrapper(original):
    def compile_runtime_policy(*, organization, profile):
        policy = original(organization=organization, profile=profile)
        section_policy = profile.get("engagement_policy") if isinstance(profile, dict) else {}
        section_policy = section_policy if isinstance(section_policy, dict) else {}

        policy["crm"] = {
            "stage_shifting": deepcopy(section_policy.get("stage_shifting") or []),
            "attribute_mapped": deepcopy(section_policy.get("attribute_mapped") or []),
            "reminders": deepcopy(section_policy.get("reminders") or []),
            "qualification_criteria": deepcopy(
                section_policy.get("qualification_criteria") or []
            ),
            "qualification_completion": str(
                section_policy.get("qualification_completion") or "configured"
            ),
        }
        qualification = policy.setdefault("qualification", {})
        qualification["source"] = str(
            ((profile.get("qualification") or {}).get("source") or "qualification_requirements")
        )

        source = json.dumps(
            policy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        policy["source_hash"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return policy

    return compile_runtime_policy


def _definition_reference_score(rule: str, definition: dict[str, Any]) -> int:
    normalized = _normalized(rule)
    key = _normalized(definition.get("key"))
    name = _normalized(definition.get("name"))
    if key and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", normalized):
        return 100
    if name and name in normalized:
        return 98
    definition_tokens = _tokens(f"{definition.get('key') or ''} {definition.get('name') or ''}")
    overlap = definition_tokens & _tokens(rule)
    if definition_tokens and definition_tokens.issubset(_tokens(rule)):
        return 92
    if overlap:
        return 70 + min(len(overlap), 4) * 4
    return 0


def _requirement_reference_score(rule: str, requirement: dict[str, Any]) -> int:
    normalized = _normalized(rule)
    priority = int(requirement.get("priority") or 0)
    if priority and re.search(rf"\bq(?:uestion)?\s*{priority}\b", normalized):
        return 100

    aliases = [
        _normalized(requirement.get("id")),
        _normalized(requirement.get("stable_id")),
        _normalized(requirement.get("label")),
    ]
    for alias in aliases:
        if alias and len(alias) >= 3 and alias in normalized:
            return 96

    requirement_tokens = _tokens(
        f"{requirement.get('label') or ''} {requirement.get('question') or ''}"
    )
    overlap = requirement_tokens & _tokens(rule)
    if len(overlap) >= 2:
        return 80 + min(len(overlap), 4) * 4
    if len(overlap) == 1 and len(requirement_tokens) <= 3:
        return 78
    return 0


def _mapped_attribute_key(
    *,
    requirement: dict[str, Any],
    definitions: list[dict[str, Any]],
    rules: list[str],
) -> str | None:
    ranked: list[tuple[int, str]] = []
    for rule in rules:
        req_score = _requirement_reference_score(rule, requirement)
        if req_score < 78:
            continue
        for definition in definitions:
            key = _clean(definition.get("key"))
            if not key:
                continue
            def_score = _definition_reference_score(rule, definition)
            if def_score < 78:
                continue
            ranked.append((req_score + def_score, key))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _merge_authored_attribute_updates(
    controlled: list[dict[str, Any]],
    *,
    context,
    projected_state: dict[str, Any],
    requirements: list[dict[str, Any]],
    runtime_policy: dict[str, Any],
    latest_message_id: str,
) -> None:
    rules = _policy_lines(runtime_policy, "attribute_mapped")
    if not rules or not latest_message_id:
        return

    definitions = [
        item
        for item in ((getattr(context, "pipeline", None) or {}).get("attribute_definitions") or [])
        if isinstance(item, dict) and _clean(item.get("key"))
    ]
    if not definitions:
        return
    by_requirement = {
        str(item.get("id") or ""): item
        for item in requirements or []
        if isinstance(item, dict) and item.get("id") is not None
    }

    updates: list[dict[str, Any]] = []
    for requirement_id, state in (projected_state.get("requirement_states") or {}).items():
        if not isinstance(state, dict):
            continue
        if _normalized(state.get("status")) != "answered":
            continue
        if str(state.get("source_message_id") or "") != latest_message_id:
            continue
        value = state.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        requirement = by_requirement.get(str(requirement_id))
        if requirement is None:
            continue
        key = _mapped_attribute_key(
            requirement=requirement,
            definitions=definitions,
            rules=rules,
        )
        if key:
            updates.append({"key": key, "value": value})

    if not updates:
        return
    existing = next(
        (item for item in controlled if item.get("type") == "attribute_updates"),
        None,
    )
    if existing is None:
        controlled.insert(0, {"type": "attribute_updates", "updates": updates})
        return
    by_key = {
        str(item.get("key")): item
        for item in existing.get("updates") or []
        if isinstance(item, dict) and item.get("key")
    }
    for item in updates:
        by_key[item["key"]] = item
    existing["updates"] = list(by_key.values())


def _stage_rule_references_destination(rule: str, destination: dict[str, Any]) -> bool:
    normalized = _normalized(rule)
    stage_name = _normalized(destination.get("name"))
    pipeline_name = _normalized(destination.get("pipeline_name"))
    if stage_name and stage_name in normalized:
        return True
    if stage_name:
        stage_tokens = _tokens(stage_name)
        if stage_tokens and stage_tokens.issubset(_tokens(rule)):
            return True
    return bool(pipeline_name and pipeline_name in normalized and stage_name in normalized)


def _condition_part(rule: str, destination: dict[str, Any]) -> str:
    text = _clean(rule)
    stage_name = _clean(destination.get("name"))
    pipeline_name = _clean(destination.get("pipeline_name"))

    patterns = [
        r"^\s*(?:if|when|whenever)\s+(?P<condition>.+?)\s*(?:,|\bthen\b)\s*(?:move|shift|route).*$",
        r"^\s*(?:move|shift|route).*?\s+if\s+(?P<condition>.+)$",
        r"^(?P<condition>.+?)\s*(?:->|=>|→)\s*.+$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean(match.group("condition"))

    stripped = text
    for value in (stage_name, pipeline_name):
        if value:
            stripped = re.sub(re.escape(value), " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(
        r"\b(?:move|shift|route|lead|user|customer|to|into|stage|pipeline|when|if|then)\b",
        " ",
        stripped,
        flags=re.IGNORECASE,
    )
    return _clean(stripped)


def _strong_evidence_match(latest_text: str, condition: str) -> bool:
    latest_tokens = _tokens(latest_text)
    condition_tokens = _tokens(condition)
    if not latest_tokens or not condition_tokens:
        return False

    overlap = latest_tokens & condition_tokens
    if condition_tokens.issubset(latest_tokens):
        return True
    if len(overlap) >= 2 and len(overlap) / len(condition_tokens) >= 0.5:
        return True

    intent_groups = (
        {"human", "talk", "support"},
        {"call", "callback", "meeting", "demo", "appointment"},
        {"seller", "sell"},
        {"buyer", "buy"},
        {"image", "screenshot"},
        {"payment", "screenshot"},
        {"not", "interested"},
    )
    for group in intent_groups:
        if condition_tokens & group and latest_tokens & group:
            condition_intent = condition_tokens & group
            latest_intent = latest_tokens & group
            if condition_intent & latest_intent:
                return True
    return False


def _authored_stage_transition(
    controlled: list[dict[str, Any]],
    *,
    decision,
    context,
    runtime_policy: dict[str, Any],
    latest_text: str,
) -> None:
    if any(item.get("type") == "pipeline_transition" for item in controlled):
        return

    rules = _policy_lines(runtime_policy, "stage_shifting")
    pipeline = getattr(context, "pipeline", None)
    stage = getattr(context, "stage", None)
    if not isinstance(pipeline, dict):
        return
    available = [
        item
        for item in pipeline.get("available_stages") or []
        if isinstance(item, dict) and item.get("id") is not None
    ]
    current_stage_id = str(stage.get("id") or "") if isinstance(stage, dict) else ""
    by_id = {str(item.get("id")): item for item in available}

    # First honor the model's semantic interpretation when the destination is
    # explicitly authored in ##Stage shifting. The backend still owns the stage
    # allow-list and never accepts an invented identifier.
    for action in getattr(decision, "crm_actions", []) or []:
        if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
            continue
        shift = action.get("stage_shift")
        stage_id = str(shift.get("stage_id") or "").strip() if isinstance(shift, dict) else ""
        destination = by_id.get(stage_id)
        if not destination or stage_id == current_stage_id:
            continue
        if _normalized(destination.get("name")) == "qualified":
            continue
        matching_rules = [
            rule for rule in rules if _stage_rule_references_destination(rule, destination)
        ]
        if matching_rules:
            controlled.append(
                {"type": "pipeline_transition", "stage_shift": {"stage_id": stage_id}}
            )
            return

    # Strong deterministic fallback for obvious authored conditions and stage
    # descriptions. This is intentionally conservative; ambiguous prose remains
    # a model proposal rather than a backend guess.
    candidates: list[tuple[int, str]] = []
    for destination in available:
        stage_id = str(destination.get("id") or "")
        if not stage_id or stage_id == current_stage_id:
            continue
        if _normalized(destination.get("name")) == "qualified":
            continue

        texts: list[str] = []
        description = _clean(destination.get("description"))
        if description:
            texts.append(description)
        texts.extend(
            rule for rule in rules if _stage_rule_references_destination(rule, destination)
        )
        for authored in texts:
            condition = _condition_part(authored, destination)
            if _strong_evidence_match(latest_text, condition):
                score = len(_tokens(latest_text) & _tokens(condition))
                candidates.append((score, stage_id))
                break

    candidates.sort(key=lambda item: (-item[0], item[1]))
    if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
        controlled.append(
            {"type": "pipeline_transition", "stage_shift": {"stage_id": candidates[0][1]}}
        )


def _action_wrapper(current_builder):
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

        from apps.ai_engagement.services.qualification_state import project_answer_updates

        projected = project_answer_updates(
            state=qualification_state,
            requirements=requirements,
            updates=getattr(decision, "qualification_updates", []) or [],
            messages=(getattr(context, "conversation", None) or {}).get("messages", []),
        )

        stage = getattr(context, "stage", None)
        stage_name = _normalized(stage.get("name") if isinstance(stage, dict) else "")
        if stage_name in {"new lead", "new leads"}:
            _merge_authored_attribute_updates(
                controlled,
                context=context,
                projected_state=projected,
                requirements=requirements,
                runtime_policy=runtime_policy,
                latest_message_id=latest_message_id,
            )

        _authored_stage_transition(
            controlled,
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            latest_text=latest_text,
        )
        return controlled, {
            **result,
            "engagement_instruction_policy_applied": True,
            "projected_qualification_state": projected,
        }

    return build


def install_engagement_instruction_runtime() -> None:
    """Turn authored Engagement Instruction headings into backend CRM policy."""

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import organization_profile as profile_module

    profile_module._profile_from_values = _profile_wrapper(
        profile_module._profile_from_values
    )

    from apps.ai_engagement.graph import runtime_policy as runtime_policy_module

    runtime_policy_module.compile_runtime_policy = _runtime_policy_wrapper(
        runtime_policy_module.compile_runtime_policy
    )

    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    policy_actions_module.build_controlled_actions = _action_wrapper(
        policy_actions_module.build_controlled_actions
    )

    _INSTALLED = True
