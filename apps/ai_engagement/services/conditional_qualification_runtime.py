from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Callable

from django.db import transaction
from django.utils import timezone


_INSTALLED = False

ELIGIBILITY_ELIGIBLE = "eligible"
ELIGIBILITY_UNKNOWN = "unknown"
ELIGIBILITY_NOT_ELIGIBLE = "not_eligible"

_RECOVERY_PHRASES = {
    "i already mentioned",
    "already mentioned",
    "i mentioned already",
    "i already told you",
    "already told you",
    "i told you already",
}

_STOPWORDS = {
    "a", "an", "and", "are", "do", "does", "did", "is", "of", "the", "to",
    "you", "your", "currently", "most", "from", "where", "what", "which", "if",
    "using", "run", "running", "use", "source", "come", "comes", "lead", "leads",
}

_CONDITIONAL_PROMPT = """
CONDITIONAL QUALIFICATION STATE
- Requirement eligibility is calculated by the backend before response generation.
- A requirement marked NOT_APPLICABLE does not exist for this conversation and must never be asked.
- A requirement marked ELIGIBLE may be asked only when it is the backend-selected current requirement.
- A conditionally ineligible requirement never blocks qualification completion.
- Optional and conditional are different concepts: optional controls completion policy; conditional controls whether the requirement exists.
- Never infer or override eligible_when rules from prose or conversation history.
- conversation_mode is backend authority. While it is QUALIFYING, generic sales CTAs must not replace or restart qualification.
- When conversation_mode is QUALIFIED, qualification questions stay disabled.
- "I already mentioned/told you" is a state-recovery signal, not a qualification answer. Do not store that phrase as the answer.
""".strip()


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized(value: Any) -> str:
    return _compact(value).casefold()


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _normalized(value)
    if text in {"yes", "y", "yeah", "yep", "true", "1", "on", "running", "using"}:
        return True
    if text in {"no", "n", "nope", "false", "0", "off", "not running", "not using"}:
        return False
    return None


def _condition_value(value: str) -> Any:
    flag = _boolish(value)
    if flag is not None:
        return flag
    return _compact(value).strip(". ,;:")


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _normalized(value))
        if token not in _STOPWORDS and len(token) > 1
    }


def _resolve_requirement_reference(
    reference: str,
    requirements: list[dict[str, Any]],
    current_index: int,
) -> str | None:
    ref = _compact(reference).strip("[]() ").casefold()
    if not ref:
        return None

    number = re.fullmatch(r"q(?:uestion)?\s*(\d+)", ref)
    if number:
        priority = int(number.group(1))
        for requirement in requirements:
            if int(requirement.get("priority") or 0) == priority:
                return str(requirement.get("id") or "") or None

    if ref.isdigit():
        priority = int(ref)
        for requirement in requirements:
            if int(requirement.get("priority") or 0) == priority:
                return str(requirement.get("id") or "") or None

    for requirement in requirements:
        aliases = {
            _normalized(requirement.get("id")),
            _normalized(requirement.get("stable_id")),
            _normalized(requirement.get("label")),
        }
        aliases.update(_normalized(item) for item in requirement.get("legacy_ids") or [])
        if ref in aliases:
            return str(requirement.get("id") or "") or None

    ref_tokens = _tokens(ref)
    if not ref_tokens:
        return None
    best: tuple[int, str] | None = None
    for index, requirement in enumerate(requirements[:current_index]):
        overlap = len(ref_tokens & _tokens(str(requirement.get("label") or "")))
        if overlap <= 0:
            continue
        candidate = (overlap * 1000 + index, str(requirement.get("id") or ""))
        if not best or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def _prior_ads_requirement(
    requirements: list[dict[str, Any]],
    current_index: int,
) -> str | None:
    for requirement in reversed(requirements[:current_index]):
        label = _normalized(requirement.get("label"))
        if any(term in label for term in ("ads", "advertising", "meta ad", "facebook ad")):
            return str(requirement.get("id") or "") or None
    return None


def _strip_optional_marker(value: str) -> str:
    return re.sub(r"\s*\(?optional\)?\s*$", "", str(value or ""), flags=re.IGNORECASE).strip()


def _condition_from_requirement(
    requirement: dict[str, Any],
    requirements: list[dict[str, Any]],
    current_index: int,
) -> tuple[dict[str, Any] | None, str | None]:
    first_line = str(requirement.get("question") or requirement.get("label") or "").splitlines()[0]

    directive = re.search(
        r"\[if\s*:\s*(?P<ref>[^=\]]+?)\s*(?:==|=|\bis\b)\s*(?P<value>[^\]]+?)\s*\]",
        first_line,
        flags=re.IGNORECASE,
    )
    if directive:
        source_id = _resolve_requirement_reference(
            directive.group("ref"), requirements, current_index
        )
        if source_id:
            cleaned = (first_line[: directive.start()] + first_line[directive.end() :]).strip()
            return {
                "requirement_id": source_id,
                "operator": "eq",
                "value": _condition_value(directive.group("value")),
            }, cleaned

    natural = re.search(
        r"\b(?:only\s+)?if\s+(?P<ref>q(?:uestion)?\s*\d+|[a-z][a-z0-9 _-]{1,60}?)\s+(?:is|equals|=)\s+(?P<value>yes|no|true|false|[a-z0-9 _-]+?)\s*$",
        first_line,
        flags=re.IGNORECASE,
    )
    if natural:
        source_id = _resolve_requirement_reference(
            natural.group("ref"), requirements, current_index
        )
        if source_id:
            return {
                "requirement_id": source_id,
                "operator": "eq",
                "value": _condition_value(natural.group("value")),
            }, first_line[: natural.start()].rstrip(" ,:-")

    not_ads = re.search(
        r"\s+if\s+(?:you(?:'re|\s+are)\s+)?not\s+(?:using|running)\s+(?:meta\s+)?ads\b.*$",
        first_line,
        flags=re.IGNORECASE,
    )
    if not_ads:
        source_id = _prior_ads_requirement(requirements, current_index)
        if source_id:
            return {
                "requirement_id": source_id,
                "operator": "eq",
                "value": False,
            }, first_line[: not_ads.start()].rstrip(" ,:-")

    using_ads = re.search(
        r"\s+if\s+(?:you(?:'re|\s+are)\s+)?(?:using|running)\s+(?:meta\s+)?ads\b.*$",
        first_line,
        flags=re.IGNORECASE,
    )
    if using_ads:
        source_id = _prior_ads_requirement(requirements, current_index)
        if source_id:
            return {
                "requirement_id": source_id,
                "operator": "eq",
                "value": True,
            }, first_line[: using_ads.start()].rstrip(" ,:-")

    return None, None


def _recompute_flow_version(mode: str, requirements: list[dict[str, Any]]) -> str:
    source = {
        "mode": mode,
        "requirements": [
            {
                "stable_id": item.get("stable_id"),
                "question": item.get("question"),
                "required": bool(item.get("required", True)),
                "options": item.get("options") or [],
                "eligible_when": item.get("eligible_when"),
            }
            for item in requirements
        ],
    }
    raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _decorate_compiled(compiled: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(compiled)
    requirements = result.get("requirements") or []
    for index, requirement in enumerate(requirements):
        requirement["ask_when_eligible"] = True
        requirement["label"] = _strip_optional_marker(str(requirement.get("label") or ""))

        question_lines = str(requirement.get("question") or "").splitlines()
        if question_lines:
            question_lines[0] = _strip_optional_marker(question_lines[0])
            requirement["question"] = "\n".join(question_lines)

        condition, cleaned_question = _condition_from_requirement(
            requirement, requirements, index
        )
        if condition is None:
            continue
        requirement["conditional"] = True
        requirement["eligible_when"] = condition
        if cleaned_question:
            lines = str(requirement.get("question") or "").splitlines()
            requirement["question"] = "\n".join([cleaned_question, *lines[1:]])
            requirement["label"] = cleaned_question.rstrip("?. ")

    version = _recompute_flow_version(str(result.get("mode") or "configured"), requirements) if requirements else ""
    result["flow_version"] = version
    for requirement in requirements:
        requirement["flow_version"] = version
    return result


def _matches_condition(actual: Any, condition: dict[str, Any]) -> bool | None:
    operator = str(condition.get("operator") or "eq").casefold()
    expected = condition.get("value")
    if operator != "eq":
        return None

    actual_bool = _boolish(actual)
    expected_bool = _boolish(expected)
    if expected_bool is not None:
        return actual_bool is expected_bool if actual_bool is not None else None
    return _normalized(actual) == _normalized(expected)


def requirement_eligibility(
    requirement: dict[str, Any],
    requirement_states: dict[str, Any],
) -> str:
    condition = requirement.get("eligible_when")
    if not isinstance(condition, dict):
        return ELIGIBILITY_ELIGIBLE
    source_id = str(condition.get("requirement_id") or "").strip()
    source = requirement_states.get(source_id) or {}
    if str(source.get("status") or "") != "answered":
        return ELIGIBILITY_UNKNOWN
    matched = _matches_condition(source.get("value"), condition)
    if matched is None:
        return ELIGIBILITY_UNKNOWN
    return ELIGIBILITY_ELIGIBLE if matched else ELIGIBILITY_NOT_ELIGIBLE


def _apply_eligibility(
    requirements: list[dict[str, Any]],
    requirement_states: dict[str, Any],
) -> dict[str, Any]:
    states = deepcopy(requirement_states or {})
    now = timezone.now().isoformat()
    for requirement in requirements or []:
        requirement_id = str(requirement.get("id") or "").strip()
        if not requirement_id:
            continue
        current = deepcopy(states.get(requirement_id) or {})
        eligibility = requirement_eligibility(requirement, states)
        current["eligibility"] = eligibility
        if eligibility == ELIGIBILITY_NOT_ELIGIBLE:
            current.update(
                {
                    "status": "not_applicable",
                    "value": None,
                    "raw_answer": None,
                    "confidence": "condition",
                    "source_message_id": None,
                    "updated_at": now,
                }
            )
        states[requirement_id] = current
    return states


def _pending_ids(
    requirements: list[dict[str, Any]],
    requirement_states: dict[str, Any],
) -> list[str]:
    pending: list[str] = []
    for requirement in sorted(
        requirements or [],
        key=lambda item: (int(item.get("priority") or 999999), str(item.get("id") or "")),
    ):
        requirement_id = str(requirement.get("id") or "").strip()
        if not requirement_id:
            continue
        eligibility = requirement_eligibility(requirement, requirement_states)
        if eligibility != ELIGIBILITY_ELIGIBLE:
            continue
        status = str((requirement_states.get(requirement_id) or {}).get("status") or "unknown")
        if status in {"unknown", "asked", "unclear"} and (
            requirement.get("required", True) or requirement.get("ask_when_eligible", False)
        ):
            pending.append(requirement_id)
    return pending


def _qualified_mode(state: dict[str, Any], lead=None) -> str:
    status = str(state.get("qualification_status") or "")
    if status == "completed":
        return "qualified"
    stage_name = _normalized(getattr(getattr(lead, "stage", None), "name", "")) if lead is not None else ""
    if any(term in stage_name for term in ("opted out", "opt out", "not interested")):
        return "opted_out"
    if any(term in stage_name for term in ("human intervention", "handoff", "human handoff")):
        return "handoff"
    if any(term in stage_name for term in ("call requested", "demo booked", "call booked")):
        return "call_requested"
    if str(state.get("engagement_mode") or "") == "qualification":
        return "qualifying"
    return "engaging"


def _atomic_lead_wrapper(original: Callable) -> Callable:
    def wrapped(lead, *args, **kwargs):
        pk = getattr(lead, "pk", None)
        manager = getattr(getattr(lead, "__class__", None), "objects", None)
        if pk is None or manager is None:
            return original(lead, *args, **kwargs)
        with transaction.atomic():
            locked = manager.select_for_update().get(pk=pk)
            result = original(locked, *args, **kwargs)
            if hasattr(locked, "attributes"):
                lead.attributes = deepcopy(locked.attributes)
            if hasattr(locked, "stage_id"):
                lead.stage_id = locked.stage_id
            return result

    return wrapped


def _atomic_keyword_lead_wrapper(original: Callable) -> Callable:
    def wrapped(*, lead, **kwargs):
        pk = getattr(lead, "pk", None)
        manager = getattr(getattr(lead, "__class__", None), "objects", None)
        if pk is None or manager is None:
            return original(lead=lead, **kwargs)
        with transaction.atomic():
            locked = manager.select_for_update().get(pk=pk)
            result = original(lead=locked, **kwargs)
            if hasattr(locked, "attributes"):
                lead.attributes = deepcopy(locked.attributes)
            if hasattr(locked, "stage_id"):
                lead.stage_id = locked.stage_id
            return result

    return wrapped


def install_conditional_qualification_runtime() -> None:
    """Install backend-owned conditional qualification and state invariants."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import organization_profile as profile_module

    original_compile = profile_module.compile_qualification_requirements

    def compile_qualification_requirements(raw: str):
        return _decorate_compiled(original_compile(raw))

    profile_module.compile_qualification_requirements = compile_qualification_requirements

    from apps.ai_engagement.services import qualification_state as state_module

    state_module._ACK_ONLY = set(state_module._ACK_ONLY) | _RECOVERY_PHRASES

    original_missing = state_module._missing_ids

    def missing_ids(requirements, requirement_states):
        states = _apply_eligibility(requirements, requirement_states)
        missing: list[str] = []
        for requirement in requirements or []:
            if not requirement.get("required", True):
                continue
            requirement_id = str(requirement.get("id") or "").strip()
            eligibility = requirement_eligibility(requirement, states)
            if eligibility == ELIGIBILITY_NOT_ELIGIBLE:
                continue
            status = str((states.get(requirement_id) or {}).get("status") or "unknown")
            if eligibility == ELIGIBILITY_UNKNOWN or status in {"unknown", "asked", "unclear"}:
                missing.append(requirement_id)
        return missing

    state_module._missing_ids = missing_ids

    def next_requirement(requirements, requirement_states):
        states = _apply_eligibility(requirements, requirement_states)
        pending = _pending_ids(requirements, states)
        if not pending:
            return None
        wanted = pending[0]
        for requirement in requirements or []:
            if str(requirement.get("id") or "") == wanted:
                return deepcopy(requirement)
        return None

    state_module.next_requirement = next_requirement

    original_normalize = state_module._normalize_runtime_state

    def normalize_runtime_state(state, requirements, *, lead=None):
        normalized = original_normalize(state, requirements, lead=lead)
        states = _apply_eligibility(requirements, normalized.get("requirement_states") or {})
        normalized["requirement_states"] = states
        normalized["missing_requirement_ids"] = missing_ids(requirements, states)
        normalized["pending_requirement_ids"] = _pending_ids(requirements, states)
        normalized["eligible_requirement_ids"] = [
            str(item.get("id"))
            for item in requirements or []
            if requirement_eligibility(item, states) == ELIGIBILITY_ELIGIBLE
        ]
        normalized["not_applicable_requirement_ids"] = [
            str(item.get("id"))
            for item in requirements or []
            if str((states.get(str(item.get("id"))) or {}).get("status") or "") == "not_applicable"
        ]
        normalized["answered_requirement_ids"] = [
            str(item.get("id"))
            for item in requirements or []
            if str((states.get(str(item.get("id"))) or {}).get("status") or "") == "answered"
        ]
        normalized["qualification_answers"] = {
            str(item.get("id")): (states.get(str(item.get("id"))) or {}).get("value")
            for item in requirements or []
            if str((states.get(str(item.get("id"))) or {}).get("status") or "") == "answered"
        }
        normalized["all_requirements_answered"] = bool(requirements) and not normalized["pending_requirement_ids"] and not normalized["missing_requirement_ids"]

        if normalized.get("qualification_status") != "completed":
            next_item = next_requirement(requirements, states)
            normalized["current_requirement_id"] = str(next_item.get("id")) if next_item else None
            normalized["next_requirement_id"] = normalized["current_requirement_id"]
        else:
            normalized["current_requirement_id"] = None
            normalized["next_requirement_id"] = None

        normalized["conversation_mode"] = _qualified_mode(normalized, lead=lead)
        return normalized

    state_module._normalize_runtime_state = normalize_runtime_state

    original_project = state_module.project_answer_updates

    def project_answer_updates(*, state, requirements, updates, messages):
        was_completed = str(state.get("qualification_status") or "") == "completed"
        projected = original_project(
            state=state,
            requirements=requirements,
            updates=updates,
            messages=messages,
        )
        projected = normalize_runtime_state(projected, requirements, lead=None)
        if not was_completed and projected.get("pending_requirement_ids"):
            projected["qualification_status"] = "in_progress"
            projected["qualification_completed"] = False
            projected["qualification_completed_at"] = None
            projected["engagement_mode"] = "qualification"
            projected["conversation_mode"] = "qualifying"
            next_item = next_requirement(requirements, projected.get("requirement_states") or {})
            projected["current_requirement_id"] = str(next_item.get("id")) if next_item else None
            projected["next_requirement_id"] = projected["current_requirement_id"]
        elif projected.get("all_requirements_answered") and requirements:
            projected["qualification_status"] = "completed"
            projected["qualification_completed"] = True
            projected["qualification_completed_at"] = projected.get("qualification_completed_at") or timezone.now().isoformat()
            projected["current_requirement_id"] = None
            projected["next_requirement_id"] = None
            projected["engagement_mode"] = "conversation"
            projected["conversation_mode"] = "qualified"
        return projected

    state_module.project_answer_updates = project_answer_updates

    original_apply = state_module.apply_unambiguous_reply

    def apply_unambiguous_reply(*, lead, requirements, text, source_message_id):
        if _normalized(text) in _RECOVERY_PHRASES:
            active_requirements = state_module.requirements_for_lead(lead, requirements)
            state = state_module.state_for_lead(lead, requirements=active_requirements)
            active_id = str(state.get("current_requirement_id") or state.get("last_asked_requirement_id") or "").strip()
            if active_id:
                recovered = None
                for event in reversed(state.get("history") or []):
                    if (
                        isinstance(event, dict)
                        and str(event.get("requirement_id") or "") == active_id
                        and event.get("event") in {"answered", "answer_corrected"}
                        and event.get("value") not in (None, "")
                    ):
                        recovered = event.get("value")
                        break
                if recovered is None:
                    requirement = next(
                        (item for item in active_requirements if str(item.get("id") or "") == active_id),
                        {},
                    )
                    attributes = getattr(lead, "attributes", {}) if isinstance(getattr(lead, "attributes", {}), dict) else {}
                    candidates = [
                        str(requirement.get("id") or ""),
                        str(requirement.get("stable_id") or ""),
                        re.sub(r"[^a-z0-9]+", "_", _normalized(requirement.get("label"))).strip("_"),
                    ]
                    for candidate in candidates:
                        if candidate and attributes.get(candidate) not in (None, ""):
                            recovered = attributes[candidate]
                            break
                if recovered is not None:
                    current = deepcopy((state.get("requirement_states") or {}).get(active_id) or {})
                    current.update(
                        {
                            "status": "answered",
                            "value": recovered,
                            "raw_answer": current.get("raw_answer") or str(recovered),
                            "confidence": "recovered",
                            "source_message_id": current.get("source_message_id"),
                            "updated_at": timezone.now().isoformat(),
                        }
                    )
                    state.setdefault("requirement_states", {})[active_id] = current
                    state = normalize_runtime_state(state, active_requirements, lead=lead)
                    state_module._persist_state(lead, state)
                    return {
                        "changed": True,
                        "state": state,
                        "answer_status": "answered",
                        "next_requirement": next_requirement(active_requirements, state.get("requirement_states") or {}),
                        "recovered": True,
                    }
            return {"changed": False, "state": state, "answer_status": None, "recovery_requested": True}
        return original_apply(
            lead=lead,
            requirements=requirements,
            text=text,
            source_message_id=source_message_id,
        )

    state_module.apply_unambiguous_reply = _atomic_keyword_lead_wrapper(apply_unambiguous_reply)
    state_module.record_last_asked_requirement = _atomic_lead_wrapper(state_module.record_last_asked_requirement)
    state_module.reset_state = _atomic_lead_wrapper(state_module.reset_state)
    state_module.persist_answer_updates = _atomic_keyword_lead_wrapper(state_module.persist_answer_updates)

    from apps.ai_engagement.graph import runtime_policy as runtime_policy_module

    original_policy_compile = runtime_policy_module.compile_runtime_policy

    def compile_runtime_policy(*, organization, profile):
        policy = original_policy_compile(organization=organization, profile=profile)
        by_id = {
            str(item.get("id") or ""): item
            for item in ((profile.get("qualification") or {}).get("requirements") or [])
            if isinstance(item, dict)
        }
        for criterion in (policy.get("qualification") or {}).get("criteria") or []:
            requirement = by_id.get(str(criterion.get("id") or "")) or {}
            if requirement.get("eligible_when"):
                criterion["conditional"] = True
                criterion["eligible_when"] = deepcopy(requirement["eligible_when"])
        source = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        policy["source_hash"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return policy

    runtime_policy_module.compile_runtime_policy = compile_runtime_policy

    from apps.ai_engagement.graph import policy_actions as policy_actions_module

    original_evaluate = policy_actions_module.evaluate_qualification

    def evaluate_qualification(*, runtime_policy, projected_state):
        result = original_evaluate(runtime_policy=runtime_policy, projected_state=projected_state)
        criteria_by_id = {
            str(item.get("id") or ""): item
            for item in ((runtime_policy.get("qualification") or {}).get("criteria") or [])
        }
        required_failed = False
        required_unknown = False
        required_missing = False
        for item in result.get("criteria") or []:
            criterion = criteria_by_id.get(str(item.get("criterion_id") or "")) or {}
            if item.get("status") == "not_applicable" and criterion.get("conditional"):
                item["verdict"] = "pass"
            if not item.get("required", True):
                continue
            if item.get("verdict") == "fail":
                required_failed = True
            elif item.get("verdict") == "unknown":
                if item.get("status") in {"unknown", "asked", "unclear"}:
                    required_missing = True
                else:
                    required_unknown = True
        if required_failed:
            result["outcome"] = "not_qualified"
        elif required_missing or required_unknown:
            result["outcome"] = "in_progress"
        elif result.get("criteria"):
            result["outcome"] = "qualified"
        return result

    policy_actions_module.evaluate_qualification = evaluate_qualification

    from apps.ai_engagement.services import engagement as engagement_module

    if _CONDITIONAL_PROMPT not in engagement_module.EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS:
        engagement_module.EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
            f"{engagement_module.EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS}\n\n{_CONDITIONAL_PROMPT}"
        )

    from apps.ai_engagement.prompts import engagement as prompt_module

    if _CONDITIONAL_PROMPT not in prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS:
        prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS = (
            f"{prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS}\n\n{_CONDITIONAL_PROMPT}"
        )

    _INSTALLED = True
