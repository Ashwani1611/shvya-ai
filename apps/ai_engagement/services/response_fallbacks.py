"""Provider-free failure wording; contains no business fact or action claim."""
from __future__ import annotations

import re


_TEXT = {
    "english": {
        "technical": "I couldn’t retrieve the answer just now. Please try your question again shortly.",
        "unverified": "I don’t have a verified answer to that specific detail.",
        "conflict": "The available details conflict, so I can’t give you a reliable answer to that detail yet.",
        "ambiguous": "Which product or service would you like details about?",
        "qualification": "Thanks for sharing that — that helps me understand your needs.",
        "reminder": "The follow-up reminder is not confirmed yet.",
        "booking": "The booking is not confirmed yet.",
        "handoff": "The handoff to a team member is not confirmed yet.",
        "file_unknown": "I couldn’t confirm whether the file was sent.",
        "file_failed": "I wasn’t able to send the file with this message.",
        "file_pending": "The file is selected, but sending is not confirmed yet.",
        "file_queued": "The file is queued for sending; delivery is not confirmed yet.",
        "qualification_pending": "The required details are not complete yet.",
    },
    "hindi": {
        "technical": "अभी उत्तर प्राप्त करने में तकनीकी समस्या आ रही है। कृपया थोड़ी देर बाद फिर कोशिश करें।",
        "unverified": "इस विशेष जानकारी का सत्यापित उत्तर अभी उपलब्ध नहीं है।",
        "conflict": "उपलब्ध जानकारी में अंतर है, इसलिए अभी इस बात का भरोसेमंद उत्तर देना संभव नहीं है।",
        "ambiguous": "आप किस उत्पाद या सेवा की जानकारी चाहते हैं?",
        "qualification": "जानकारी देने के लिए धन्यवाद। इससे आपकी ज़रूरत समझने में मदद मिली।",
        "reminder": "फ़ॉलो-अप रिमाइंडर की अभी पुष्टि नहीं हुई है।",
        "booking": "बुकिंग की अभी पुष्टि नहीं हुई है।",
        "handoff": "टीम के सदस्य को बातचीत सौंपे जाने की अभी पुष्टि नहीं हुई है।",
        "file_unknown": "फ़ाइल भेजी गई है या नहीं, इसकी पुष्टि नहीं हो पाई।",
        "file_failed": "इस संदेश के साथ फ़ाइल नहीं भेजी जा सकी।",
        "file_pending": "फ़ाइल चुनी गई है, लेकिन भेजे जाने की अभी पुष्टि नहीं हुई है।",
        "file_queued": "फ़ाइल भेजने की कतार में है; डिलीवरी की अभी पुष्टि नहीं हुई है।",
        "qualification_pending": "ज़रूरी जानकारी अभी पूरी नहीं हुई है।",
    },
    "hinglish": {
        "technical": "Abhi jawab retrieve karne mein technical problem aa rahi hai. Kripya thodi der baad phir try karein.",
        "unverified": "Is specific detail ka verified jawab abhi available nahi hai.",
        "conflict": "Available details mein farq hai, isliye abhi reliable jawab confirm nahi ho pa raha.",
        "ambiguous": "Aap kis product ya service ki details chahte hain?",
        "qualification": "Details share karne ke liye thank you. Aapki zaroorat samajhne mein madad mili.",
        "reminder": "Follow-up reminder abhi confirm nahi hua hai.",
        "booking": "Booking abhi confirm nahi hui hai.",
        "handoff": "Team member ko handoff abhi confirm nahi hua hai.",
        "file_unknown": "File send hui hai ya nahi, abhi confirm nahi ho pa raha.",
        "file_failed": "Is message ke saath file send nahi ho paayi.",
        "file_pending": "File select hui hai, lekin sending abhi confirm nahi hui hai.",
        "file_queued": "File sending queue mein hai; delivery abhi confirm nahi hui hai.",
        "qualification_pending": "Zaroori details abhi complete nahi hui hain.",
    },
}
# A neutral uncertainty sentence remains available without a second provider
# call for additional configured languages. It asserts neither absence nor an
# outage, and never pretends to confirm an action.
_NEUTRAL = {
    "spanish": "No puedo confirmar esa información en este momento.",
    "french": "Je ne peux pas confirmer cette information pour le moment.",
    "german": "Ich kann diese Information derzeit nicht bestätigen.",
    "portuguese": "Não consigo confirmar essa informação neste momento.",
    "arabic": "لا أستطيع تأكيد هذه المعلومات في الوقت الحالي.",
    "bengali": "এই মুহূর্তে এই তথ্যটি নিশ্চিত করতে পারছি না।",
    "marathi": "या क्षणी या माहितीची पुष्टी करता येत नाही.",
    "tamil": "இந்தத் தகவலை இப்போது உறுதிப்படுத்த முடியவில்லை.",
    "telugu": "ప్రస్తుతం ఈ సమాచారాన్ని నిర్ధారించలేకపోతున్నాను.",
    "gujarati": "હાલમાં આ માહિતીની પુષ્ટિ કરી શકાતી નથી.",
    "kannada": "ಈ ಮಾಹಿತಿಯನ್ನು ಈಗ ಖಚಿತಪಡಿಸಲು ಸಾಧ್ಯವಾಗುತ್ತಿಲ್ಲ.",
    "malayalam": "ഈ വിവരം ഇപ്പോൾ സ്ഥിരീകരിക്കാൻ കഴിയുന്നില്ല.",
    "punjabi": "ਇਸ ਸਮੇਂ ਇਸ ਜਾਣਕਾਰੀ ਦੀ ਪੁਸ਼ਟੀ ਨਹੀਂ ਕਰ ਸਕਦੇ।",
}


def response_language(organization_context=None, latest_text=""):
    data = organization_context if isinstance(organization_context, dict) else {}
    raw = str(data.get("bot_languages") or "").casefold()
    aliases = {"en": "english", "hi": "hindi", "हिन्दी": "hindi", "हिंदी": "hindi"}
    configured = [aliases.get(item.strip(), item.strip()) for item in re.split(r"[,;/\n]+", raw) if item.strip()]
    if "hindi" in configured and re.search(r"[\u0900-\u097f]", str(latest_text)):
        return "hindi"
    if "hinglish" in configured and re.search(r"\b(?:hai|hain|kya|kaise|chahiye|bhejo|kitna)\b", str(latest_text), re.I):
        return "hinglish"
    if configured:
        return configured[0]
    return "hindi" if re.search(r"[\u0900-\u097f]", str(latest_text)) else "english"


def failure_text(kind="technical", *, organization_context=None, latest_text="", language=None):
    selected = language or response_language(organization_context, latest_text)
    if selected in _TEXT:
        return _TEXT[selected].get(kind, _TEXT[selected]["technical"])
    return _NEUTRAL.get(selected, _TEXT["english"]["technical"])


def grounding_failure_text(state, *, reason="", qualification_turn=False):
    context = state.get("context")
    kind = "qualification" if qualification_turn else "technical"
    coverage = state.get("evidence_coverage")
    status = str(getattr(coverage, "status", ""))
    if not qualification_turn and reason in {"missing_evidence", "unanswered_question"}:
        if status in {"conflicting", "conflict"}:
            kind = "conflict"
        elif status == "ambiguous":
            kind = "ambiguous"
        elif status in {"insufficient", "partial"}:
            kind = "unverified"
    return failure_text(kind, organization_context=getattr(context, "organization", {}),
                        latest_text=state.get("latest_text", ""))


def organization_failure_text(organization, kind="technical", *, latest_text=""):
    """Optional metadata read must not break an already failing provider turn."""
    from django.db import transaction
    from apps.ai_engagement.models import OrgInfo
    data = {}
    try:
        with transaction.atomic():
            data = OrgInfo.objects.filter(organization=organization).values("bot_languages").first() or {}
    except Exception:
        pass
    return failure_text(kind, organization_context=data, latest_text=latest_text)
