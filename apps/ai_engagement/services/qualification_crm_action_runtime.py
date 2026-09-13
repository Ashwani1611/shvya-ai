from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone


_INSTALLED = False
_NEW_LEAD_STAGE_NAMES = {"new lead", "new leads"}
_TERMINAL_REQUIREMENT_STATES = {"answered", "skipped", "not_applicable"}

_CONNECT_REQUEST_RE = re.compile(
    r"\b(?:call\s+me|call\s+back|callback|contact\s+me|connect\s+me|"
    r"want\s+to\s+connect|would\s+like\s+to\s+connect|speak\s+with|talk\s+to|"
    r"discuss\s+with|human\s+(?:help|assistance|support)|book\s+(?:a\s+)?(?:call|demo|meeting)|"
    r"schedule\s+(?:a\s+)?(?:call|demo|meeting))\b",
    flags=re.IGNORECASE,
)
_SCHEDULING_CONTEXT_RE = re.compile(
    r"\b(?:call|callback|contact|connect|demo|meeting|appointment|session|follow\s*up|remind|schedule|book)\b",
    flags=re.IGNORECASE,
)
_TIME_12H_RE = re.compile(r"\b(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>am|pm)\b", re.IGNORECASE)
_TIME_24H_RE = re.compile(r"\b(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\b")
_ISO_DATE_RE = re.compile(r"\b(?P<year>20\d{2})[-/](?P<month>0?[1-9]|1[0-2])[-/](?P<day>0?[1-9]|[12]\d|3[01])\b")
_DMY_DATE_RE = re.compile(r"\b(?P<day>0?[1-9]|[12]\d|3[01])[-/](?P<month>0?[1-9]|1[0-2])[-/](?P<year>20\d{2})\b")

_STOP_TOKENS = {
    "a", "an", "and", "are", "be", "do", "does", "for", "from", "how",
    "in", "is", "of", "or", "per", "the", "to", "what", "where", "which",
    "who", "your", "you", "currently", "current", "most", "now", "right",
    "lead", "leads",
}
_TOKEN_ALIASES = {
    "ads": "ad",
    "advertising": "ad",
    "advertisement": "ad",
    "advertisements": "ad",
    "daily": "day",
    "management": "manage",
    "managing": "manage",
    "managed": "manage",
    "sources": "source",
    "origin": "source",
    "origins": "source",
    "come": "source",
    "comes": "source",
    "coming": "source",
    "challenges": "challenge",
    "converting": "convert",
    "conversion": "convert",
    "converted": "convert",
    "replies": "reply",
    "tracking": "track",
    "tracked": "track",
    "walkins": "walkin",
    "referrals": "referral",
    "runs": "run",
    "running": "run",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _stage_name(context) -> str:
    stage = getattr(context, "stage", None)
    if not isinstance(stage, dict):
        return ""
    return _clean(stage.get("name")).casefold()


def _latest_inbound(context) -> tuple[str, str]:
    conversation = getattr(context, "conversation", None)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("direction") != "inbound":
            continue
        body = str(message.get("body") or "").strip()
        if body:
            return str(message.get("id") or "").strip(), body
    return "", ""


def _tokens(value: Any) -> set[str]:
    raw_tokens = re.findall(r"[a-z0-9]+", _clean(value).casefold())
    normalized: set[str] = set()
    for token in raw_tokens:
        token = _TOKEN_ALIASES.get(token, token)
        if token in _STOP_TOKENS:
            continue
        normalized.add(token)
    return normalized


def _option_values(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    result: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            rendered = item.get("value")
        else:
            rendered = item
        normalized = _clean(rendered).casefold()
        if normalized:
            result.add(normalized)
    return result


def _attribute_match_score(definition: dict[str, Any], requirement: dict[str, Any]) -> float:
    key = _clean(definition.get("key")).casefold()
    name = _clean(definition.get("name")).casefold()
    requirement_id = _clean(requirement.get("id")).casefold()
    label = _clean(requirement.get("label")).casefold()
    question = _clean(requirement.get("question")).casefold()

    if key and key == requirement_id:
        return 100.0
    if name and label and name == label:
        return 98.0

    requirement_tokens = _tokens(f"{label} {question}")
    name_tokens = _tokens(f"{definition.get('key') or ''} {definition.get('name') or ''}")
    description_tokens = _tokens(definition.get("description"))

    score = 0.0
    if name_tokens:
        overlap = name_tokens & requirement_tokens
        coverage = len(overlap) / len(name_tokens)
        if name_tokens.issubset(requirement_tokens):
            score = max(score, 90.0 + min(len(name_tokens), 5))
        elif coverage >= 0.67:
            score = max(score, 78.0 + 10.0 * coverage)
        elif len(overlap) >= 2 and coverage >= 0.5:
            score = max(score, 72.0)
        elif len(name_tokens) == 1 and overlap:
            score = max(score, 76.0)

    if description_tokens:
        overlap = description_tokens & requirement_tokens
        coverage = len(overlap) / len(description_tokens)
        if len(overlap) >= 2 and coverage >= 0.5:
            score = max(score, 74.0)

    requirement_options = _option_values(requirement.get("options"))
    definition_options = _option_values(definition.get("options"))
    if requirement_options and definition_options:
        overlap = requirement_options & definition_options
        if overlap:
            coverage = len(overlap) / min(len(requirement_options), len(definition_options))
            if len(overlap) >= 2 and coverage >= 0.5:
                score = max(score, 96.0)
            elif coverage == 1.0:
                score = max(score, 94.0)

    return score


def _matching_attribute_key(*, requirement: dict[str, Any], definitions: list[dict[str, Any]], used: set[str]) -> str | None:
    ranked: list[tuple[float, str]] = []
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        key = _clean(definition.get("key"))
        if not key or key in used:
            continue
        ranked.append((_attribute_match_score(definition, requirement), key))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked or ranked[0][0] < 75.0:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0] and ranked[0][0] < 94.0:
        return None
    return ranked[0][1]


def _turn_answer_updates(*, qualification_state: dict[str, Any], latest_message_id: str, latest_text: str) -> list[dict[str, Any]]:
    if not latest_message_id:
        return []
    updates: list[dict[str, Any]] = []
    states = qualification_state.get("requirement_states") or {}
    for requirement_id, state in states.items():
        if not isinstance(state, dict):
            continue
        if str(state.get("status") or "").casefold() != "answered":
            continue
        if str(state.get("source_message_id") or "") != latest_message_id:
            continue
        value = state.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        evidence = str(state.get("raw_answer") or latest_text or "").strip()
        if not evidence:
            continue
        updates.append({
            "requirement_id": str(requirement_id),
            "value": value,
            "source_message_id": latest_message_id,
            "evidence": evidence,
        })
    return updates


def _deterministic_attribute_updates(*, turn_updates, requirements, context) -> list[dict[str, Any]]:
    definitions = [
        item
        for item in ((getattr(context, "pipeline", None) or {}).get("attribute_definitions") or [])
        if isinstance(item, dict) and _clean(item.get("key"))
    ]
    if not definitions:
        return []
    requirements_by_id = {
        str(item.get("id") or ""): item
        for item in requirements or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    used: set[str] = set()
    result: list[dict[str, Any]] = []
    for update in turn_updates or []:
        requirement = requirements_by_id.get(str(update.get("requirement_id") or ""))
        if requirement is None:
            continue
        key = _matching_attribute_key(requirement=requirement, definitions=definitions, used=used)
        if not key:
            continue
        used.add(key)
        result.append({"key": key, "value": update.get("value")})
    return result


def _merge_attribute_updates(controlled: list[dict[str, Any]], updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    existing = next((item for item in controlled if item.get("type") == "attribute_updates"), None)
    if existing is None:
        controlled.insert(0, {"type": "attribute_updates", "updates": updates})
        return
    by_key = {
        str(item.get("key")): item
        for item in existing.get("updates") or []
        if isinstance(item, dict) and item.get("key")
    }
    for item in updates:
        by_key[str(item["key"])] = item
    existing["updates"] = list(by_key.values())


def _parse_grounded_due_at(text: str) -> str | None:
    normalized = _clean(text).casefold()
    if not normalized:
        return None

    now = timezone.localtime(timezone.now())
    target_date = None
    if "tomorrow" in normalized:
        target_date = now.date() + timedelta(days=1)
    elif "today" in normalized or "tonight" in normalized:
        target_date = now.date()
    else:
        match = _ISO_DATE_RE.search(normalized) or _DMY_DATE_RE.search(normalized)
        if match:
            try:
                target_date = datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                ).date()
            except ValueError:
                target_date = None

    hour = minute = None
    match12 = _TIME_12H_RE.search(normalized)
    if match12:
        hour = int(match12.group("hour"))
        minute = int(match12.group("minute") or 0)
        if match12.group("ampm").casefold() == "pm" and hour != 12:
            hour += 12
        if match12.group("ampm").casefold() == "am" and hour == 12:
            hour = 0
    else:
        match24 = _TIME_24H_RE.search(normalized)
        if match24:
            hour = int(match24.group("hour"))
            minute = int(match24.group("minute"))

    if hour is None:
        return None
    if target_date is None:
        target_date = now.date()
        candidate = timezone.make_aware(
            datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute or 0),
            timezone.get_current_timezone(),
        )
        if candidate <= now:
            target_date = target_date + timedelta(days=1)

    naive = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute or 0)
    aware = timezone.make_aware(naive, timezone.get_current_timezone())
    return aware.isoformat()


def _ensure_reminder(controlled: list[dict[str, Any]], latest_text: str) -> None:
    explicit_connect = bool(_CONNECT_REQUEST_RE.search(latest_text or ""))
    parsed_due_at = _parse_grounded_due_at(latest_text)
    scheduling_context = bool(_SCHEDULING_CONTEXT_RE.search(latest_text or ""))

    existing = next((item for item in controlled if item.get("type") == "create_reminder"), None)
    if existing is not None:
        if parsed_due_at:
            existing["due_at"] = parsed_due_at
        elif explicit_connect:
            existing["due_at"] = timezone.now().isoformat()
        return

    if not explicit_connect and not (parsed_due_at and scheduling_context):
        return

    due_at = parsed_due_at or timezone.now().isoformat()
    controlled.append({
        "type": "create_reminder",
        "title": "Follow up with lead",
        "description": "Lead requested a call, demo, meeting, or follow-up.",
        "due_at": due_at,
    })


def _all_requirements_terminal(qualification_state: dict[str, Any], requirements: list[dict[str, Any]]) -> bool:
    if not requirements:
        return False
    states = qualification_state.get("requirement_states") or {}
    for requirement in requirements:
        if not requirement.get("required", True):
            continue
        requirement_id = str(requirement.get("id") or "")
        status = str((states.get(requirement_id) or {}).get("status") or "").casefold()
        if status not in _TERMINAL_REQUIREMENT_STATES:
            return False
    return True


def _wrap_controlled_actions(current_builder, policy_actions_module):
    def build(
        *,
        decision,
        context,
        runtime_policy: dict[str, Any],
        qualification_state: dict[str, Any],
        requirements: list[dict[str, Any]],
    ):
        controlled, result = current_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        controlled = [dict(item) for item in controlled]

        latest_message_id, latest_text = _latest_inbound(context)
        stage_name = _stage_name(context)
        explicit_stage = bool(stage_name)
        in_new_lead = stage_name in _NEW_LEAD_STAGE_NAMES

        # Production AIContext always carries stage metadata. Only New Lead may
        # consume qualification state for CRM enrichment/completion. Synthetic
        # contexts without a stage keep the existing builder behavior unchanged.
        if explicit_stage and in_new_lead:
            turn_updates = _turn_answer_updates(
                qualification_state=qualification_state,
                latest_message_id=latest_message_id,
                latest_text=latest_text,
            )
            deterministic_attrs = _deterministic_attribute_updates(
                turn_updates=turn_updates,
                requirements=requirements,
                context=context,
            )
            _merge_attribute_updates(controlled, deterministic_attrs)

            evaluation = policy_actions_module.evaluate_qualification(
                runtime_policy=runtime_policy,
                projected_state=qualification_state,
            )
            result = {**result, "evaluation": evaluation}

            qualified_stage_id = str(qualification_state.get("qualified_stage_id") or "").strip()
            if (
                qualified_stage_id
                and _all_requirements_terminal(qualification_state, requirements)
                and evaluation.get("outcome") == "qualified"
            ):
                # Qualification completion has priority over any model-proposed
                # stage move on the same New Lead turn.
                controlled = [
                    item for item in controlled if item.get("type") != "pipeline_transition"
                ]
                controlled.append({
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": qualified_stage_id},
                })
                result = {
                    **result,
                    "stage_transition": {
                        "stage_id": qualified_stage_id,
                        "source": "qualification_complete",
                    },
                }

        _ensure_reminder(controlled, latest_text)
        return controlled, result

    return build


def install_qualification_crm_action_runtime() -> None:
    """Bridge persisted qualification answers into deterministic CRM actions.

    Deterministic extraction can persist a qualification answer before LangGraph
    plans CRM actions. This wrapper makes that already-verified answer visible to
    attribute mapping and final Qualified-stage progression without reopening
    qualification outside New Lead.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    current_builder = policy_actions_module.build_controlled_actions
    policy_actions_module.build_controlled_actions = _wrap_controlled_actions(
        current_builder,
        policy_actions_module,
    )
    _INSTALLED = True
