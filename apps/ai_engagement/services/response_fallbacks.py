"""Finite last-resort replies with no invented business or execution facts.

Normal generation and the independently verified wording repair remain primary.
These templates never echo untrusted questions/source text or claim a retry,
callback or handoff has been scheduled. An unsuccessful search proves only that
this turn could not verify a detail, not that the company has no such information.
"""
from __future__ import annotations

import re


_MESSAGES = {
    "english": {
        "technical": "I couldn’t retrieve the answer just now. Please try your question again shortly.",
        "unverified": "I couldn’t verify that specific detail from the information available here.",
        "pricing": "I couldn’t verify the price for that option from the information available here.",
        "policy": "I couldn’t verify the policy for that situation from the information available here.",
        "ambiguous": "Which product, plan or service are you asking about?",
        "conflicting": "The available information gives conflicting details, so I can’t confirm that point yet.",
        "action": "I couldn’t confirm that the requested action was completed.",
        "qualification": "Thanks for sharing that — that helps me understand your needs.",
    },
    "hindi": {
        "technical": "अभी मैं आपके सवाल का जवाब प्राप्त नहीं कर पाया। कृपया थोड़ी देर बाद दोबारा पूछें।",
        "unverified": "यहाँ उपलब्ध जानकारी से मैं इस खास बात की पुष्टि नहीं कर पाया।",
        "pricing": "यहाँ उपलब्ध जानकारी से मैं उस विकल्प की कीमत की पुष्टि नहीं कर पाया।",
        "policy": "यहाँ उपलब्ध जानकारी से मैं उस स्थिति पर लागू नीति की पुष्टि नहीं कर पाया।",
        "ambiguous": "आप किस उत्पाद, प्लान या सेवा के बारे में पूछ रहे हैं?",
        "conflicting": "उपलब्ध जानकारी में अलग-अलग विवरण हैं, इसलिए अभी मैं इस बात की पुष्टि नहीं कर सकता।",
        "action": "मैं यह पुष्टि नहीं कर पाया कि अनुरोधित काम पूरा हुआ है।",
        "qualification": "जानकारी देने के लिए धन्यवाद — इससे आपकी ज़रूरत समझने में मदद मिली।",
    },
    "hinglish": {
        "technical": "Abhi aapke sawaal ka jawab retrieve nahi ho paaya. Kripya thodi der baad dobara poochhein.",
        "unverified": "Yahan available information se main is specific detail ko verify nahi kar paaya.",
        "pricing": "Yahan available information se main us option ki price verify nahi kar paaya.",
        "policy": "Yahan available information se main us situation ki policy verify nahi kar paaya.",
        "ambiguous": "Aap kis product, plan ya service ke baare mein poochh rahe hain?",
        "conflicting": "Available information mein alag-alag details hain, isliye abhi is point ko confirm nahi kar sakta.",
        "action": "Main confirm nahi kar paaya ki requested action complete hua hai.",
        "qualification": "Details share karne ke liye thanks — aapki need samajhne mein madad mili.",
    },
}
TECHNICAL_FAILURES = frozenset({
    "storage_error", "retrieval_error", "embedding_error", "timeout", "budget_exhausted",
    "source_failed", "source_not_ready", "index_empty", "provider_configuration_error",
    "provider_rejected", "provider_temporary_error", "provider_error", "repair_failed",
})


def fallback_language(bot_languages="", latest_text=""):
    configured = [item.strip().casefold() for item in re.split(r"[,;\n/|]+", str(bot_languages or "")) if item.strip()]
    configured = ["hindi" if item in {"हिन्दी", "हिंदी"} else item for item in configured]
    if not configured:
        return "hindi" if re.search(r"[\u0900-\u097f]", str(latest_text)) else "english"
    if "hindi" in configured and re.search(r"[\u0900-\u097f]", str(latest_text)):
        return "hindi"
    return next((item for item in configured if item in _MESSAGES), "english")


def fallback_message(*, kind="technical", bot_languages="", latest_text="", question_type=""):
    if kind == "unverified" and question_type in {"pricing", "policy"}:
        kind = question_type
    messages = _MESSAGES[fallback_language(bot_languages, latest_text)]
    return messages.get(kind, messages["technical"])


def failure_kind(state, *, reason=""):
    coverage = state.get("evidence_coverage")
    status = str(getattr(coverage, "status", "") or "")
    if reason in TECHNICAL_FAILURES or state.get("retrieval_status") in TECHNICAL_FAILURES or status in TECHNICAL_FAILURES:
        return "technical"
    if status in {"ambiguous", "conflicting"}:
        return status
    if reason == "unperformed_action":
        return "action"
    if status in {"insufficient", "partial"}:
        return "unverified"
    return "technical"
