"""Conservative, provider-free grounding fast paths shared by the AI runtime.

Word overlap, numeric overlap and substring matches are not entailment. Only a
complete evidence item (or one bounded grammatical rendering of a scalar price)
may skip semantic verification. No normalization removes numbers or negation.
"""
from __future__ import annotations

import re
import unicodedata

_PRICE = re.compile(r"[₹$€£]\s*\d[\d,]*(?:\.\d+)?(?:\s*/\s*(?:month|year|day|week|hour))?", re.I)
_SAFE_ACKNOWLEDGEMENTS = frozenset({
    "hi", "hello", "thanks", "thank you", "thanks for sharing that",
    "thank you for sharing that", "you're welcome", "you’re welcome",
    "how can i help you", "how can i help you today", "namaste", "dhanyavaad",
    "नमस्ते", "धन्यवाद", "शुक्रिया",
})


def normalized_text(value) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


def language_only(decision) -> bool:
    return not (
        getattr(decision, "crm_actions", None)
        or getattr(decision, "qualification_updates", None)
        or getattr(decision, "file_document_id", None) is not None
    )


def exact_evidence_reply(decision, resolution, *, allow_price_template=False) -> bool:
    if resolution is None or not getattr(resolution, "verified", False):
        return False
    if not language_only(decision):
        return False
    if str(getattr(resolution, "question_type", "")) not in {
        "pricing", "policy", "location", "working_hours", "product_or_service",
    }:
        return False
    reply = normalized_text(getattr(decision, "message", ""))
    if not reply:
        return False
    # Static evidence may contain templates/instructions, not proof that an
    # operational action occurred. Keep such replies on the independent guard.
    if re.search(r"(?:booking|appointment|payment|handoff|refund).{0,35}(?:confirmed|completed)|"
                 r"\b(?:i|we|have)\s+(?:have\s+)?(?:booked|scheduled|sent|notified|assigned|reserved)|"
                 r"ignore (?:all |previous )?instructions|system prompt|api[_ ]key|access[_ ]token|password|"
                 r"बुकिंग.{0,20}(?:पक्की|कन्फर्म)", reply, re.I):
        return False
    for item in getattr(resolution, "evidence", ()) or ():
        content = normalized_text(getattr(item, "content", ""))
        if content and reply == content:
            return True
        # This finite template preserves the entire single scalar price. It does
        # not permit appended discounts, guarantees, dates, or conditional text.
        if (allow_price_template and getattr(resolution, "question_type", "") == "pricing"
                and _PRICE.fullmatch(content)
                and reply in {f"our price is {content}.", f"the price is {content}."}):
            return True
    return False


def safe_acknowledgement(decision, resolution) -> bool:
    if getattr(decision, "reason_code", "") != "NORMAL_CONVERSATION" or not language_only(decision):
        return False
    if resolution is not None and (getattr(resolution, "sensitive", False)
            or getattr(resolution, "question_type", "") not in {"", "not_evidence_bound"}):
        return False
    return normalized_text(getattr(decision, "message", "")).rstrip(".!?।") in _SAFE_ACKNOWLEDGEMENTS
