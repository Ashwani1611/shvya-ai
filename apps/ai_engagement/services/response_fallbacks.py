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
        "unverified": "I want to give you the right detail. Which product, plan or service are you referring to?",
        "pricing": "I don’t have the price for that option in the configured details yet. Which plan or setup are you asking about?",
        "policy": "Which product or situation should I check the policy for?",
        "ambiguous": "Which product, plan or service are you asking about?",
        "conflicting": "The configured information is conflicting for that detail. Which option are you referring to?",
        "action": "That action is not confirmed yet. I can continue helping here while it is checked.",
        "qualification": "Thanks for sharing that — that helps me understand your needs.",
    },
    "hindi": {
        "technical": "अभी जवाब निकालने में अस्थायी समस्या आई। कृपया अपना सवाल एक बार फिर भेजें।",
        "unverified": "मैं सही जानकारी देना चाहता हूँ। आप किस उत्पाद, प्लान या सेवा की बात कर रहे हैं?",
        "pricing": "आप किस प्लान या सेटअप की कीमत पूछ रहे हैं? मैं उसी विकल्प की कॉन्फ़िगर की गई कीमत बताऊँगा।",
        "policy": "आप किस उत्पाद या स्थिति की नीति जानना चाहते हैं?",
        "ambiguous": "आप किस उत्पाद, प्लान या सेवा के बारे में पूछ रहे हैं?",
        "conflicting": "इस विषय पर एक से अधिक कॉन्फ़िगर की गई जानकारियाँ हैं। आप किस विकल्प की बात कर रहे हैं?",
        "action": "वह कार्रवाई अभी पूरी होने की पुष्टि नहीं हुई है। तब तक मैं यहीं आपकी मदद जारी रख सकता हूँ।",
        "qualification": "जानकारी देने के लिए धन्यवाद — इससे आपकी ज़रूरत समझने में मदद मिली।",
    },
    "hinglish": {
        "technical": "Abhi answer retrieve nahi ho paaya. Aap ek baar sawaal dobara bhej den?",
        "unverified": "Main sahi detail dena chahta hoon. Aap kis product, plan ya service ki baat kar rahe hain?",
        "pricing": "Aap kis plan ya setup ki pricing poochh rahe hain? Main usi option ki configured pricing use karunga.",
        "policy": "Aap kis product ya situation ki policy check karna chahte hain?",
        "ambiguous": "Aap kis product, plan ya service ke baare mein poochh rahe hain?",
        "conflicting": "Is point ke liye multiple configured details mil rahi hain. Aap kis option ki baat kar rahe hain?",
        "action": "Woh action abhi confirmed nahi hai. Tab tak main yahin aapki help continue kar sakta hoon.",
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
