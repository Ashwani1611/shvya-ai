"""Cheap grounding checks that preserve complete assertions, not just tokens.

A matching number or bag of words is not evidence of entailment. Only an entire
verified evidence item, or a tightly constrained rendering of a scalar price,
can bypass the existing verifier. No provider call is introduced here.
"""
from __future__ import annotations

import re
from typing import Any


_SCALAR_PRICE = re.compile(
    r"[₹$€£]\s*[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?"
    r"(?:\s*(?:/|per\s+)(?:day|week|month|year))?",
    re.IGNORECASE,
)
_SOCIAL_REPLIES = frozenset({
    "hi", "hello", "hey", "thanks", "thank you", "you're welcome",
    "thanks for sharing", "thanks for sharing that", "thank you for sharing",
    "how can i help?", "how can i help you?", "namaste", "नमस्ते", "धन्यवाद",
})


def normalize_assertion(value: Any) -> str:
    # Do not discard digits, negation, punctuation, units, or word order.
    return " ".join(str(value or "").split()).casefold()


def language_only(decision: Any) -> bool:
    return not (
        getattr(decision, "crm_actions", None)
        or getattr(decision, "qualification_updates", None)
        or getattr(decision, "file_document_id", None) is not None
    )


def matches_verified_evidence(
    decision: Any, resolution: Any, *, allow_scalar_price_template: bool = False,
) -> bool:
    if resolution is None or not getattr(resolution, "verified", False):
        return False
    if not language_only(decision):
        return False
    reply = normalize_assertion(getattr(decision, "message", ""))
    if not reply:
        return False
    for item in getattr(resolution, "evidence", ()) or ():
        evidence = normalize_assertion(getattr(item, "content", ""))
        if not evidence:
            continue
        if reply == evidence:
            return True
        # This deliberately does not accept fragments of a policy, multiple
        # plans, conditions, or a correct price followed by an invented claim.
        if (
            allow_scalar_price_template
            and str(getattr(resolution, "question_type", "")) == "pricing"
            and _SCALAR_PRICE.fullmatch(evidence)
            and reply in {
                f"our price is {evidence}.",
                f"the price is {evidence}.",
                f"our price is {evidence}",
                f"the price is {evidence}",
            }
        ):
            return True
    return False


def is_social_only_reply(decision: Any, resolution: Any) -> bool:
    if getattr(decision, "reason_code", "") != "NORMAL_CONVERSATION":
        return False
    if not language_only(decision):
        return False
    if resolution is not None and (
        getattr(resolution, "sensitive", False)
        or str(getattr(resolution, "question_type", "") or "")
        not in {"", "not_evidence_bound"}
    ):
        return False
    # An allowlist, not absence of a few risky keywords. Unknown wording goes
    # through the canonical verifier rather than being assumed non-factual.
    reply = normalize_assertion(getattr(decision, "message", "")).rstrip(".!")
    return reply in _SOCIAL_REPLIES
