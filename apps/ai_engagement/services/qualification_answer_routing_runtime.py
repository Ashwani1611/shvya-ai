from __future__ import annotations

import re
import sys
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
_INTERMITTENT_POSITIVE_BOOLEAN_RE = re.compile(
    r"\b(?:sometimes?|some\s+times?|occasionally|at\s+times|from\s+time\s+to\s+time|"
    r"on\s+and\s+off|off\s+and\s+on|once\s+in\s+a\s+while|rarely)\b",
    flags=re.IGNORECASE,
)
_OPTION_KEY_RE = re.compile(
    r"^\s*(?:option\s+)?(?P<key>[a-z]|\d{1,2})\s*[\)\].:\-]?\s*$",
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
    normalized = (
        _normalized(value)
        .replace(",", "")
        .replace("₹", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
    )
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
    normalized = (
        _normalized(value)
        .replace(",", "")
        .replace("₹", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
    )
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)", normalized)
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


def _number_in_range(
    number: float,
    bounds: tuple[float | None, float | None, bool, bool],
) -> bool:
    lower, upper, include_lower, include_upper = bounds
    if lower is not None and (number < lower or (number == lower and not include_lower)):
        return False
    if upper is not None and (number > upper or (number == upper and not include_upper)):
        return False
    return True


def _match_key_option(text: str, options: list[dict[str, str]]) -> str | None:
    match = _OPTION_KEY_RE.match(str(text or ""))
    if match is None:
        return None
    supplied = match.group("key").casefold()
    for index, option in enumerate(options, start=1):
        key = str(option.get("key") or "").strip().casefold()
        value = _clean(option.get("value"))
        aliases = {key, str(index)}
        if index <= 26:
            aliases.add(chr(96 + index))
        if supplied in aliases and value:
            return value
    return None


def _numeric_option_candidates(text: str, options: list[dict[str, str]]) -> list[str]:
    number = _extract_number(text)
    if number is None:
        return []
    return [
        _clean(option.get("value"))
        for option in options
        if _range_for_option(_clean(option.get("value")))
        and _number_in_range(number, _range_for_option(_clean(option.get("value"))))
    ]


def _match_numeric_option(text: str, options: list[dict[str, str]]) -> str | None:
    candidates = _numeric_option_candidates(text, options)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1 and "+" in _normalized(text):
        plus_candidates = [item for item in candidates if "+" in _normalized(item)]
        if len(plus_candidates) == 1:
            return plus_candidates[0]
    return None


def _text_option_candidates(text: str, options: list[dict[str, str]]) -> list[str]:
    normalized_text = _normalized(text)
    if not normalized_text:
        return []

    exact = [
        _clean(option.get("value"))
        for option in options
        if _normalized(option.get("value")) == normalized_text
    ]
    if exact:
        return list(dict.fromkeys(exact))

    input_tokens = _tokens(text)
    if not input_tokens:
        return []

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
        if len(option_tokens) > 1 and coverage < 0.5:
            continue
        ranked.append((coverage, len(overlap), value))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return list(dict.fromkeys(item[2] for item in ranked))


def _match_text_option(text: str, options: list[dict[str, str]]) -> str | None:
    candidates = _text_option_candidates(text, options)
    return candidates[0] if len(candidates) == 1 else None


def _explicit_multi_text_option(text: str, options: list[dict[str, str]]) -> str | None:
    """Accept clearly positive multi-option text without guessing contradictions."""
    normalized = _normalized(text)
    if not normalized or _NEGATIVE_RE.search(normalized):
        return None
    if not re.search(r"(?:\band\b|&|,|/)", normalized):
        return None
    candidates = set(_text_option_candidates(text, options))
    if len(candidates) < 2:
        return None
    ordered = [
        _clean(option.get("value"))
        for option in options
        if _clean(option.get("value")) in candidates
    ]
    ordered = list(dict.fromkeys(item for item in ordered if item))
    return "; ".join(ordered) if len(ordered) >= 2 else None


def _match_boolean_option(
    text: str,
    question: str,
    options: list[dict[str, str]],
) -> str | None:
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
    # Natural frequency answers still mean "Yes" for a binary current-ads
    # requirement. Keep this deterministic so replies such as "some time"
    # advance qualification instead of falling through to the grounding gate.
    if _INTERMITTENT_POSITIVE_BOOLEAN_RE.search(normalized):
        return by_value["yes"]

    question_tokens = _tokens(str(question or "").splitlines()[0])
    text_tokens = _tokens(text)
    if question_tokens and not (question_tokens & text_tokens):
        return None
    if _NEGATIVE_RE.search(text):
        return by_value["no"]
    if _POSITIVE_BOOLEAN_RE.search(text):
        return by_value["yes"]
    return None


def _ambiguous_option_answer(
    text: str,
    question: str,
    options: list[dict[str, str]],
) -> bool:
    """Return True only when the lead substantively selects multiple options."""
    normalized_values = {
        _normalized(option.get("value"))
        for option in options
        if _clean(option.get("value"))
    }
    if normalized_values == {"yes", "no"}:
        normalized = _normalized(text)
        positive = bool(re.search(r"\b(?:yes|yeah|yep)\b", normalized))
        negative = bool(re.search(r"\b(?:no|nope)\b", normalized))
        if positive and negative:
            return True

    numeric_candidates = _numeric_option_candidates(text, options)
    if len(set(numeric_candidates)) > 1 and "+" not in _normalized(text):
        return True

    text_candidates = _text_option_candidates(text, options)
    return len(set(text_candidates)) > 1


def _enhanced_direct_classifier(original, state_module):
    def classify(*, text: str, question: str):
        classified = original(text=text, question=question)
        raw_text = str(text or "").strip()
        options = state_module._question_options(question)
        if classified is not None and classified[0] == state_module.REQUIREMENT_ANSWERED:
            # Preserve exact authored text, option keys, and deterministic
            # multi-tool answers such as "Chats and CRM" -> "Multiple places".
            # Only override a bare numeric scalar when authored ranges overlap
            # at that number (for example 100-500 and 500-2,000).
            scalar_number = bool(
                re.fullmatch(
                    r"\s*[₹$€£]?\s*\d+(?:[.,]\d+)?\s*",
                    raw_text,
                )
            )
            if (
                scalar_number
                and options
                and len(set(_numeric_option_candidates(raw_text, options))) > 1
            ):
                return (state_module.REQUIREMENT_UNCLEAR, raw_text, "high")
            return classified

        if not raw_text or len(raw_text) > 240 or "\n" in raw_text:
            return classified
        # Mixed informational questions stay on the model/RAG path so one turn
        # can answer the customer question and update qualification state.
        if "?" in raw_text:
            return classified

        if not options:
            return classified

        scalar_number = bool(
            re.fullmatch(
                r"\s*[₹$€£]?\s*\d+(?:[.,]\d+)?\s*",
                raw_text,
            )
        )
        numeric_candidates = _numeric_option_candidates(raw_text, options)
        if scalar_number and len(set(numeric_candidates)) > 1:
            return (state_module.REQUIREMENT_UNCLEAR, raw_text, "high")

        matched = (
            _match_key_option(raw_text, options)
            or _match_boolean_option(raw_text, question, options)
            or _match_numeric_option(raw_text, options)
            or (None if scalar_number else _match_text_option(raw_text, options))
            or (None if scalar_number else _explicit_multi_text_option(raw_text, options))
        )
        if matched is not None:
            return (state_module.REQUIREMENT_ANSWERED, matched, "high")

        if _ambiguous_option_answer(raw_text, question, options):
            return (state_module.REQUIREMENT_UNCLEAR, raw_text, "high")

        return classified

    return classify


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


def _is_safe_backend_question(state, persisted: dict[str, Any]) -> bool:
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


def _qualification_first_grounding(original):
    def check(state):
        has_requirements = bool(state.get("requirements") or [])
        persisted_answer = _latest_persisted_answer(state)
        if persisted_answer is not None:
            _, persisted = persisted_answer
            if _is_safe_backend_question(state, persisted):
                return {
                    "grounding_approved": True,
                    "qualification_answer_authoritative": True,
                }

        result = original(state)
        if has_requirements or not isinstance(result, dict):
            return result

        qualification_bypass = bool(
            result.get("qualification_answer_authoritative")
            or result.get("grounding_recovered")
        )
        if not qualification_bypass:
            return result

        decision = state.get("decision")
        if decision is None:
            return {"grounding_approved": False}

        from apps.ai_engagement.graph import evidence as evidence_module

        return {
            "decision": evidence_module._safe_unknown_decision(decision),
            "grounding_approved": False,
        }

    return check


def install_qualification_answer_routing_runtime() -> None:
    """Prioritize the active configured requirement without owning CRM state."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_state as state_module

    state_module._classify_direct_reply = _enhanced_direct_classifier(
        state_module._classify_direct_reply,
        state_module,
    )

    from apps.ai_engagement.graph import evidence as evidence_module

    evidence_module.check_grounding = _qualification_first_grounding(
        evidence_module.check_grounding
    )

    workflow_name = "apps.ai_engagement.graph.workflow"
    workflow_module = sys.modules.get(workflow_name)
    if workflow_module is not None:
        workflow_module.check_grounding = evidence_module.check_grounding
        workflow_module.ENGAGEMENT_GRAPH = workflow_module.build_engagement_graph()

    _INSTALLED = True