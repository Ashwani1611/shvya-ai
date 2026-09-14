from __future__ import annotations

import re
import sys
from dataclasses import replace
from typing import Any


_INSTALLED = False
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-"})
_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_TOKEN_ALIASES = {
    "ads": "ad",
    "advertising": "ad",
    "advertisements": "ad",
    "chats": "chat",
    "daily": "day",
    "leads": "lead",
    "managed": "manage",
    "management": "manage",
    "managing": "manage",
    "many": "count",
    "number": "count",
    "quantity": "count",
    "received": "receive",
    "receives": "receive",
    "receiving": "receive",
    "referrals": "referral",
    "sheets": "sheet",
    "sources": "source",
}
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
    "how",
    "i",
    "in",
    "is",
    "it",
    "mostly",
    "of",
    "on",
    "or",
    "per",
    "the",
    "through",
    "to",
    "typically",
    "we",
    "what",
    "where",
    "which",
    "with",
    "you",
    "your",
}
_NEGATIVE_RE = re.compile(
    r"\b(?:no|nope|not|never|don't|dont|do\s+not|doesn't|doesnt|does\s+not|without)\b",
    flags=re.IGNORECASE,
)
_POSITIVE_BOOLEAN_RE = re.compile(
    r"\b(?:yes|yeah|yep|run|running|use|using|have|currently)\b",
    flags=re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").translate(_DASHES)).strip()


def _normalized(value: Any) -> str:
    return _clean(value).casefold().strip(" .,:;!?()[]{}\"'")


def _tokens(value: Any) -> set[str]:
    result: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", _normalized(value)):
        token = _TOKEN_ALIASES.get(raw, raw)
        if token and token not in _STOP_TOKENS:
            result.add(token)
    return result


def _extract_number(value: str) -> float | None:
    normalized = _normalized(value)
    match = re.search(r"(?<![a-z])\d+(?:\.\d+)?(?![a-z])", normalized)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None

    words = re.findall(r"[a-z]+", normalized)
    for index, word in enumerate(words):
        if word not in _NUMBER_WORDS:
            continue
        number = _NUMBER_WORDS[word]
        if number >= 20 and number % 10 == 0 and index + 1 < len(words):
            tail = _NUMBER_WORDS.get(words[index + 1])
            if tail is not None and 0 < tail < 10:
                number += tail
        return float(number)
    return None


def _range_for_option(value: str) -> tuple[float | None, float | None, bool, bool] | None:
    normalized = _normalized(value)
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", normalized)
    if match:
        return float(match.group(1)), float(match.group(2)), True, True

    match = re.search(r"(\d+(?:\.\d+)?)\s*\+", normalized)
    if match:
        return float(match.group(1)), None, True, True

    match = re.search(r"(?:more\s+than|over|above|greater\s+than)\s*(\d+(?:\.\d+)?)", normalized)
    if match:
        return float(match.group(1)), None, False, True

    match = re.search(r"(?:up\s*to|upto|at\s+most|<=?)\s*(\d+(?:\.\d+)?)", normalized)
    if match:
        return None, float(match.group(1)), True, True

    match = re.search(r"(?:less\s+than|under|below)\s*(\d+(?:\.\d+)?)", normalized)
    if match:
        return None, float(match.group(1)), True, False
    return None


def _number_in_range(number: float, bounds: tuple[float | None, float | None, bool, bool]) -> bool:
    lower, upper, include_lower, include_upper = bounds
    if lower is not None and (number < lower or (number == lower and not include_lower)):
        return False
    if upper is not None and (number > upper or (number == upper and not include_upper)):
        return False
    return True


def _match_numeric_option(text: str, options: list[dict[str, str]]) -> str | None:
    number = _extract_number(text)
    if number is None:
        return None

    candidates: list[str] = []
    normalized_text = _normalized(text)
    for option in options:
        value = _clean(option.get("value"))
        bounds = _range_for_option(value)
        if bounds and _number_in_range(number, bounds):
            candidates.append(value)

    if len(candidates) == 1:
        return candidates[0]

    # Shared boundaries such as 30 in "10-30" and "30+" are ambiguous unless
    # the lead explicitly used the authored plus form.
    if len(candidates) > 1 and "+" in normalized_text:
        plus_candidates = [item for item in candidates if "+" in _normalized(item)]
        if len(plus_candidates) == 1:
            return plus_candidates[0]
    return None


def _match_text_option(text: str, options: list[dict[str, str]]) -> str | None:
    normalized_text = _normalized(text)
    if not normalized_text:
        return None

    # Normalize unicode dashes before exact authored-value matching.
    exact = [
        _clean(option.get("value"))
        for option in options
        if _normalized(option.get("value")) == normalized_text
    ]
    if len(exact) == 1:
        return exact[0]

    input_tokens = _tokens(text)
    if not input_tokens:
        return None

    ranked: list[tuple[float, int, str]] = []
    for option in options:
        value = _clean(option.get("value"))
        option_tokens = _tokens(value)
        if not option_tokens:
            continue
        overlap = input_tokens & option_tokens
        if not overlap:
            continue
        coverage = len(overlap) / len(option_tokens)
        if coverage < 0.5 and len(option_tokens) > 1:
            continue
        ranked.append((coverage, len(overlap), value))

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
        return None
    return ranked[0][2]


def _match_boolean_option(text: str, question: str, options: list[dict[str, str]]) -> str | None:
    by_value = {
        _normalized(option.get("value")): _clean(option.get("value"))
        for option in options
        if _clean(option.get("value"))
    }
    if set(by_value) != {"yes", "no"}:
        return None

    normalized = _normalized(text)
    if re.match(r"^(?:yes|yeah|yep)\b", normalized):
        return by_value["yes"]
    if re.match(r"^(?:no|nope)\b", normalized):
        return by_value["no"]

    question_tokens = _tokens(str(question or "").splitlines()[0])
    text_tokens = _tokens(text)
    if question_tokens and not (question_tokens & text_tokens):
        return None
    if _NEGATIVE_RE.search(text):
        return by_value["no"]
    if _POSITIVE_BOOLEAN_RE.search(text):
        return by_value["yes"]
    return None


def _enhanced_direct_classifier(original, state_module):
    def classify(*, text: str, question: str):
        classified = original(text=text, question=question)
        if classified is not None:
            return classified

        raw_text = str(text or "").strip()
        if not raw_text or len(raw_text) > 240 or "\n" in raw_text:
            return None
        # A mixed informational question should stay on the normal engagement
        # path so it can be answered as well as qualified by the model. This
        # deterministic path is only for a clear answer to the active question.
        if "?" in raw_text:
            return None

        options = state_module._question_options(question)
        if not options:
            return None

        matched = (
            _match_boolean_option(raw_text, question, options)
            or _match_numeric_option(raw_text, options)
            or _match_text_option(raw_text, options)
        )
        if matched is None:
            return None
        return (state_module.REQUIREMENT_ANSWERED, matched, "high")

    return classify


def _semantic_attribute_score(original):
    def score(definition: dict[str, Any], requirement: dict[str, Any]) -> float:
        current = float(original(definition, requirement))
        if current >= 75.0:
            return current

        requirement_tokens = _tokens(
            f"{requirement.get('label') or ''} {requirement.get('question') or ''}"
        )
        definition_tokens = _tokens(
            f"{definition.get('name') or ''} {definition.get('description') or ''}"
        )
        if not requirement_tokens or not definition_tokens:
            return current

        overlap = requirement_tokens & definition_tokens
        coverage = len(overlap) / min(len(requirement_tokens), len(definition_tokens))
        if len(overlap) >= 3 and coverage >= 0.5:
            return max(current, 88.0)
        if len(overlap) >= 2 and coverage >= 0.4:
            return max(current, 82.0)
        return current

    return score


def _latest_persisted_answer(state) -> tuple[str, dict[str, Any]] | None:
    latest_id = str(state.get("latest_message_id") or "").strip()
    lead = state.get("lead")
    requirements = state.get("requirements") or []
    if not latest_id or lead is None:
        return None

    try:
        from apps.ai_engagement.services.qualification_state import state_for_lead

        persisted = state_for_lead(lead, requirements=requirements)
    except Exception:
        return None

    for requirement_id, item in (persisted.get("requirement_states") or {}).items():
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").casefold() != "answered":
            continue
        if str(item.get("source_message_id") or "") == latest_id:
            return str(requirement_id), persisted
    return None


def _next_requirement(state, qualification_state: dict[str, Any]) -> dict[str, Any] | None:
    next_id = str(qualification_state.get("next_requirement_id") or "").strip()
    if not next_id:
        return None
    return next(
        (
            item
            for item in state.get("requirements") or []
            if str(item.get("id") or "").strip() == next_id
        ),
        None,
    )


def _is_safe_deterministic_qualification_reply(state, persisted: dict[str, Any]) -> bool:
    decision = state.get("decision")
    if decision is None or str(getattr(decision, "model", "")) != "deterministic":
        return False
    if str(getattr(decision, "reason_code", "") or "").upper() != "QUALIFICATION_NEXT":
        return False

    next_item = _next_requirement(state, persisted)
    if not isinstance(next_item, dict):
        return False
    next_id = str(next_item.get("id") or "").strip()
    if str(getattr(decision, "next_requirement_id", "") or "").strip() != next_id:
        return False

    question = str(next_item.get("question") or "").strip()
    message = str(getattr(decision, "message", "") or "").strip()
    return bool(question and message.endswith(question))


def _qualification_safe_recovery(state, persisted: dict[str, Any]):
    decision = state.get("decision")
    if decision is None:
        return None

    next_item = _next_requirement(state, persisted)
    if isinstance(next_item, dict) and str(next_item.get("question") or "").strip():
        question = str(next_item["question"]).strip()
        return replace(
            decision,
            should_engage=True,
            message=f"Got it. {question}",
            file_document_id=None,
            qualification_updates=[],
            next_requirement_id=str(next_item.get("id") or "") or None,
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            model="deterministic-recovery",
        )

    if str(persisted.get("qualification_status") or "").casefold() == "completed":
        return replace(
            decision,
            should_engage=True,
            message="Thanks — I’ve got the information I need.",
            file_document_id=None,
            qualification_updates=[],
            next_requirement_id=None,
            reason="NORMAL_CONVERSATION",
            reason_code="NORMAL_CONVERSATION",
            model="deterministic-recovery",
        )
    return None


def _qualification_first_grounding(original):
    def check(state):
        persisted_answer = _latest_persisted_answer(state)
        if persisted_answer is not None:
            _, persisted = persisted_answer
            # This reply contains only a generic acknowledgement plus the exact
            # backend-selected authored question. Sending it to a second model
            # for factual grounding can only introduce false rejection/fallback.
            if _is_safe_deterministic_qualification_reply(state, persisted):
                return {
                    "grounding_approved": True,
                    "qualification_answer_authoritative": True,
                }

        result = original(state)
        if persisted_answer is None or result.get("grounding_approved") is not False:
            return result

        _, persisted = persisted_answer
        recovery = _qualification_safe_recovery(state, persisted)
        if recovery is None:
            return result
        return {
            "decision": recovery,
            "grounding_approved": True,
            "qualification_answer_authoritative": True,
            "grounding_recovered": True,
        }

    return check


def install_qualification_answer_routing_runtime() -> None:
    """Make active qualification answers authoritative before generic grounding.

    This patch is intentionally narrow:
    - deterministic matching is scoped to the persisted active requirement;
    - natural option/range/boolean equivalents normalize to authored values;
    - attribute descriptions can strengthen an otherwise weak mapping;
    - a persisted qualification answer cannot be replaced by the generic
      unknown-information fallback merely because a second grounding model
      rejects the acknowledgement + next-question wording.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_state as state_module

    state_module._classify_direct_reply = _enhanced_direct_classifier(
        state_module._classify_direct_reply,
        state_module,
    )

    from apps.ai_engagement.services import qualification_crm_action_runtime as crm_runtime

    crm_runtime._attribute_match_score = _semantic_attribute_score(
        crm_runtime._attribute_match_score
    )

    from apps.ai_engagement.graph import evidence as evidence_module

    evidence_module.check_grounding = _qualification_first_grounding(
        evidence_module.check_grounding
    )

    # If a test/import loaded workflow unusually early, rebuild the compiled
    # graph so its grounding node receives the patched function as well.
    workflow_name = "apps.ai_engagement.graph.workflow"
    workflow_module = sys.modules.get(workflow_name)
    if workflow_module is not None:
        workflow_module.check_grounding = evidence_module.check_grounding
        workflow_module.ENGAGEMENT_GRAPH = workflow_module.build_engagement_graph()

    _INSTALLED = True
