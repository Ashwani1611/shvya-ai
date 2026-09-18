"""Cheap grounding proofs; uncertain replies keep the existing verifier path."""
from __future__ import annotations

import re


def _normalized(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def exact_evidence_reply(decision, resolution, *, allow_price_wrapper=False) -> bool:
    """Prove equality with one COMPLETE verified evidence item, never word overlap."""
    if resolution is None or not getattr(resolution, "verified", False):
        return False
    if getattr(decision, "crm_actions", None) or getattr(decision, "qualification_updates", None):
        return False
    if getattr(decision, "file_document_id", None) is not None:
        return False
    reply = _normalized(getattr(decision, "message", ""))
    if not reply:
        return False
    for item in (getattr(resolution, "evidence", ()) or ()):
        content = _normalized(getattr(item, "content", ""))
        if content and reply == content:
            return True
        # A fixed grammatical wrapper around a COMPLETE bare price is safe;
        # arbitrary prefixes, suffixes, discounts and condition deletion are not.
        if (allow_price_wrapper and getattr(resolution, "question_type", "") == "pricing"
                and re.fullmatch(r"(?:₹|\$|€|£|inr\s+|usd\s+)[0-9][0-9,.]*(?:\s*(?:/|per\s+)(?:month|year|day|week))?", content)):
            if reply in {f"our price is {content}.", f"our price is {content}"}:
                return True
    return False


def factual_free_acknowledgement(decision, resolution) -> bool:
    """Closed factual-free language set, not a risky-word blacklist or composer."""
    if str(getattr(decision, "reason_code", "")) != "NORMAL_CONVERSATION":
        return False
    if getattr(decision, "crm_actions", None) or getattr(decision, "qualification_updates", None):
        return False
    if getattr(decision, "file_document_id", None) is not None:
        return False
    if resolution is not None and (
        getattr(resolution, "sensitive", False)
        or str(getattr(resolution, "question_type", "")) not in {"", "not_evidence_bound"}
    ):
        return False
    text = _normalized(getattr(decision, "message", "")).strip(".!?। ")
    return text in {
        "hi", "hello", "thanks", "thank you", "you're welcome", "you are welcome",
        "thanks for sharing", "thanks for sharing that", "thank you for sharing", "understood", "noted",
        "how can i help", "how can i help you", "what can i help you with",
        "नमस्ते", "धन्यवाद", "शुक्रिया", "समझ गया", "samajh gaya", "shukriya",
    }
