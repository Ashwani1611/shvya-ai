"""Reject deterministic contradictions without replacing semantic interpretation.

A quote's presence in an inbound message proves its origin, not the option chosen
by the model. These checks reject only unambiguous option/value conflicts. Other
languages, paraphrases and ambiguous statements retain the independent grounding
review; this module does not infer or authorize answers.
"""
from __future__ import annotations

import re

from apps.ai_engagement.services.qualification_answer_routing_runtime import (
    _match_numeric_option,
    _normalized,
    _range_for_option,
    _tokens,
)

_NEGATIVE = re.compile(r"\b(?:not|never|don't|dont|doesn't|doesnt|no longer)\b", re.I)


def _source_clause(evidence, source):
    # Retain surrounding negation when a model quotes only a positive fragment.
    # Repeated fragments spanning different clauses cannot prove one meaning.
    clauses = [part for part in re.split(r"[.!?;\n।]+", source) if evidence in part]
    return clauses[0].strip() if len(clauses) == 1 else evidence.strip()


def contradicts_option_evidence(*, requirement, value, evidence, source):
    from apps.ai_engagement.services.qualification_state import _question_options

    options = requirement.get("options") or _question_options(requirement.get("question") or "")
    options = [item for item in options if isinstance(item, dict) and item.get("value")]
    authored = {_normalized(item["value"]): item["value"] for item in options}
    selected = _normalized(value)
    if selected not in authored:
        # Existing contracts may use raw numbers or multiple authored answers.
        # This guard is concerned only with contradictory single-option values.
        return False
    clause = _source_clause(evidence, source)
    normalized = _normalized(clause)
    quote = _normalized(evidence)
    question = (str(requirement.get("question") or requirement.get("label") or "").splitlines() or [""])[0]

    if set(authored) == {"yes", "no"}:
        # Conflicting/corrected polarities and compound clauses need semantics.
        if _NEGATIVE.search(question) or re.search(r"\b(?:but|however|actually|correction)\b", normalized):
            return False
        if re.search(r"\b(?:and|or|used to|plan|planning|sometimes|occasionally)\b", normalized):
            return False
        if re.search(r"\b(?:yes|yeah|yep)\b", normalized) and re.search(r"\b(?:no|nope)\b", normalized):
            return False
        explicit = re.match(r"^(yes|yeah|yep|no|nope)(?:\s*[,!:.]|\s*$)", quote)
        if explicit and not _NEGATIVE.search(normalized):
            answer = "yes" if explicit.group(1) in {"yes", "yeah", "yep"} else "no"
            return selected != answer
        topic = _tokens(question) - {"currently", "now", "run", "running", "use", "using", "have", "has", "am"}
        if not (topic & _tokens(clause)):
            return False
        if re.search(
            r"\b(?:not\s+(?:(?:currently|now|actively)\s+)?(?:running|using)|"
            r"(?:do not|does not|don't|doesn't)\s+(?:run|use|have))\b", normalized,
        ):
            return selected != "no"
        # Presence of "currently" plus a verb does not prove affirmation:
        # "we currently have no paid ads" and "we aren't running paid ads"
        # are negatives. Non-explicit patterns retain semantic interpretation.
        return False

    if options and all(_range_for_option(item["value"]) for item in options):
        # One explicit scalar may select exactly one authored band. Do not
        # reinterpret estimates, ranges, phone numbers or differing periods.
        numbers = re.findall(r"(?<!\w)\d+(?:\.\d+)?(?!\w)", normalized)
        if len(numbers) != 1 or re.search(r"\b(?:over|under|above|below|more|less|between)\b|\+", normalized):
            return False
        if re.search(r"\b(?:month|monthly|week|weekly|year|yearly)\b", normalized):
            return False
        if not (_tokens(question) & _tokens(evidence)) and quote != _normalized(source):
            return False
        expected = _match_numeric_option(quote, options)
        return expected is not None and selected != _normalized(expected)

    matched = set()
    for option in options:
        label = str(option["value"])
        for alias in [label, *re.split(r"\s*/\s*", label)]:
            alias = _normalized(alias)
            if len(alias) < 3:
                continue
            match = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", normalized)
            if match is not None:
                matched.add(_normalized(label))
    if len(matched) == 1 and re.search(r"\b(?:and|or)\b|[&,/]", normalized):
        # A conjunction can name a second option by a shortened label, e.g.
        # "WhatsApp and Excel" for "WhatsApp chats" / "Excel / Sheets".
        # Partial words establish ambiguity only, never an accepted answer.
        known_tokens = _tokens(next(iter(matched))) | _tokens(question)
        source_tokens = _tokens(clause)
        if any(
            (_tokens(option["value"]) - known_tokens) & source_tokens
            for option in options if _normalized(option["value"]) not in matched
        ):
            return False
    # Negation, corrections and multiple mentioned options require semantic
    # review. A plain unique authored meaning cannot become another option.
    if _NEGATIVE.search(normalized) or re.search(r"\b(?:but|however|actually|correction)\b", normalized):
        return False
    return len(matched) == 1 and selected not in matched
