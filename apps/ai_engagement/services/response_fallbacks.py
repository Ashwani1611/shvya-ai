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
    "punjabi": {
        "technical": "ਇਸ ਵੇਲੇ ਜਵਾਬ ਪ੍ਰਾਪਤ ਨਹੀਂ ਹੋ ਸਕਿਆ। ਕਿਰਪਾ ਕਰਕੇ ਕੁਝ ਦੇਰ ਬਾਅਦ ਆਪਣਾ ਸਵਾਲ ਦੁਬਾਰਾ ਭੇਜੋ।",
        "unverified": "ਸਹੀ ਜਾਣਕਾਰੀ ਦੇਣ ਲਈ, ਕਿਰਪਾ ਕਰਕੇ ਦੱਸੋ ਕਿ ਤੁਸੀਂ ਕਿਸ ਉਤਪਾਦ, ਪਲਾਨ ਜਾਂ ਸੇਵਾ ਬਾਰੇ ਪੁੱਛ ਰਹੇ ਹੋ?",
        "pricing": "ਉਸ ਵਿਕਲਪ ਦੀ ਕੀਮਤ ਉਪਲਬਧ ਜਾਣਕਾਰੀ ਵਿੱਚ ਨਹੀਂ ਮਿਲੀ। ਤੁਸੀਂ ਕਿਸ ਪਲਾਨ ਜਾਂ ਸੈੱਟਅੱਪ ਦੀ ਕੀਮਤ ਪੁੱਛ ਰਹੇ ਹੋ?",
        "policy": "ਤੁਸੀਂ ਕਿਸ ਉਤਪਾਦ ਜਾਂ ਸਥਿਤੀ ਲਈ ਨੀਤੀ ਬਾਰੇ ਜਾਣਨਾ ਚਾਹੁੰਦੇ ਹੋ?",
        "ambiguous": "ਤੁਸੀਂ ਕਿਸ ਉਤਪਾਦ, ਪਲਾਨ ਜਾਂ ਸੇਵਾ ਬਾਰੇ ਪੁੱਛ ਰਹੇ ਹੋ?",
        "conflicting": "ਇਸ ਵੇਰਵੇ ਬਾਰੇ ਉਪਲਬਧ ਜਾਣਕਾਰੀ ਆਪਸ ਵਿੱਚ ਮੇਲ ਨਹੀਂ ਖਾਂਦੀ। ਤੁਸੀਂ ਕਿਸ ਵਿਕਲਪ ਬਾਰੇ ਪੁੱਛ ਰਹੇ ਹੋ?",
        "action": "ਉਹ ਕਾਰਵਾਈ ਪੂਰੀ ਹੋਣ ਦੀ ਹਾਲੇ ਪੁਸ਼ਟੀ ਨਹੀਂ ਹੋਈ। ਮੈਂ ਇੱਥੇ ਤੁਹਾਡੀ ਮਦਦ ਜਾਰੀ ਰੱਖ ਸਕਦਾ ਹਾਂ।",
        "qualification": "ਜਾਣਕਾਰੀ ਸਾਂਝੀ ਕਰਨ ਲਈ ਧੰਨਵਾਦ — ਇਸ ਨਾਲ ਤੁਹਾਡੀਆਂ ਲੋੜਾਂ ਸਮਝਣ ਵਿੱਚ ਮਦਦ ਮਿਲੀ।",
    },
    "marathi": {
        "technical": "आत्ता उत्तर मिळवता आले नाही. कृपया थोड्या वेळाने तुमचा प्रश्न पुन्हा पाठवा.",
        "unverified": "योग्य माहिती देण्यासाठी, कृपया तुम्ही कोणते उत्पादन, प्लॅन किंवा सेवा याबद्दल विचारत आहात ते सांगा.",
        "pricing": "त्या पर्यायाची किंमत उपलब्ध माहितीत सापडली नाही. तुम्ही कोणत्या प्लॅन किंवा सेटअपची किंमत विचारत आहात?",
        "policy": "तुम्हाला कोणत्या उत्पादनासाठी किंवा परिस्थितीसाठी धोरण जाणून घ्यायचे आहे?",
        "ambiguous": "तुम्ही कोणते उत्पादन, प्लॅन किंवा सेवा याबद्दल विचारत आहात?",
        "conflicting": "या तपशिलाबद्दल उपलब्ध माहितीत विसंगती आहे. तुम्ही कोणत्या पर्यायाबद्दल विचारत आहात?",
        "action": "ती कृती पूर्ण झाल्याची अद्याप पुष्टी झालेली नाही. मी इथे तुमची मदत सुरू ठेवू शकतो.",
        "qualification": "माहिती दिल्याबद्दल धन्यवाद — त्यामुळे तुमच्या गरजा समजून घेण्यास मदत झाली.",
    },
    "german": {
        "technical": "Ich konnte die Antwort gerade nicht abrufen. Bitte stellen Sie Ihre Frage in Kürze erneut.",
        "unverified": "Damit ich Ihnen die richtige Auskunft geben kann: Welches Produkt, welchen Tarif oder welche Dienstleistung meinen Sie?",
        "pricing": "In den verfügbaren Angaben habe ich keinen Preis für diese Option gefunden. Welchen Tarif oder welche Konfiguration meinen Sie?",
        "policy": "Für welches Produkt oder welche Situation möchten Sie die geltenden Bedingungen wissen?",
        "ambiguous": "Welches Produkt, welchen Tarif oder welche Dienstleistung meinen Sie?",
        "conflicting": "Die verfügbaren Angaben zu diesem Detail widersprechen sich. Welche Option meinen Sie?",
        "action": "Es ist noch nicht bestätigt, dass diese Aktion abgeschlossen wurde. Ich kann Ihnen hier weiterhin helfen.",
        "qualification": "Vielen Dank für die Angaben — sie helfen mir, Ihren Bedarf zu verstehen.",
    },
    "kannada": {
        "technical": "ಈಗ ಉತ್ತರವನ್ನು ಪಡೆಯಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ನಿಮ್ಮ ಪ್ರಶ್ನೆಯನ್ನು ಮತ್ತೆ ಕಳುಹಿಸಿ.",
        "unverified": "ಸರಿಯಾದ ಮಾಹಿತಿ ನೀಡಲು, ನೀವು ಯಾವ ಉತ್ಪನ್ನ, ಪ್ಲಾನ್ ಅಥವಾ ಸೇವೆಯ ಬಗ್ಗೆ ಕೇಳುತ್ತಿದ್ದೀರಿ ಎಂದು ತಿಳಿಸಿ.",
        "pricing": "ಆ ಆಯ್ಕೆಯ ಬೆಲೆ ಲಭ್ಯವಿರುವ ಮಾಹಿತಿಯಲ್ಲಿ ಸಿಗಲಿಲ್ಲ. ನೀವು ಯಾವ ಪ್ಲಾನ್ ಅಥವಾ ವ್ಯವಸ್ಥೆಯ ಬೆಲೆಯನ್ನು ಕೇಳುತ್ತಿದ್ದೀರಿ?",
        "policy": "ಯಾವ ಉತ್ಪನ್ನ ಅಥವಾ ಸಂದರ್ಭಕ್ಕೆ ಸಂಬಂಧಿಸಿದ ನೀತಿಯ ಬಗ್ಗೆ ತಿಳಿಯಲು ಬಯಸುತ್ತೀರಿ?",
        "ambiguous": "ನೀವು ಯಾವ ಉತ್ಪನ್ನ, ಪ್ಲಾನ್ ಅಥವಾ ಸೇವೆಯ ಬಗ್ಗೆ ಕೇಳುತ್ತಿದ್ದೀರಿ?",
        "conflicting": "ಈ ವಿವರಕ್ಕೆ ಸಂಬಂಧಿಸಿದ ಲಭ್ಯ ಮಾಹಿತಿಯಲ್ಲಿ ವಿರೋಧಾಭಾಸವಿದೆ. ನೀವು ಯಾವ ಆಯ್ಕೆಯ ಬಗ್ಗೆ ಕೇಳುತ್ತಿದ್ದೀರಿ?",
        "action": "ಆ ಕಾರ್ಯ ಪೂರ್ಣಗೊಂಡಿರುವುದು ಇನ್ನೂ ದೃಢಪಟ್ಟಿಲ್ಲ. ನಾನು ಇಲ್ಲಿಯೇ ನಿಮಗೆ ಸಹಾಯ ಮಾಡುವುದನ್ನು ಮುಂದುವರಿಸಬಹುದು.",
        "qualification": "ಮಾಹಿತಿ ಹಂಚಿಕೊಂಡಿದ್ದಕ್ಕಾಗಿ ಧನ್ಯವಾದಗಳು — ನಿಮ್ಮ ಅಗತ್ಯಗಳನ್ನು ಅರ್ಥಮಾಡಿಕೊಳ್ಳಲು ಇದು ಸಹಾಯವಾಯಿತು.",
    },
}

_LANGUAGE_ALIASES = {
    "en": "english", "en-us": "english", "en-gb": "english",
    "hi": "hindi", "hi-in": "hindi", "हिन्दी": "hindi", "हिंदी": "hindi",
    "pa": "punjabi", "pa-in": "punjabi", "panjabi": "punjabi", "ਪੰਜਾਬੀ": "punjabi",
    "mr": "marathi", "mr-in": "marathi", "मराठी": "marathi",
    "de": "german", "de-de": "german", "de-at": "german", "de-ch": "german", "deutsch": "german",
    "kn": "kannada", "kn-in": "kannada", "ಕನ್ನಡ": "kannada",
}
TECHNICAL_FAILURES = frozenset({
    "storage_error", "retrieval_error", "embedding_error", "timeout", "budget_exhausted",
    "source_failed", "source_not_ready", "index_empty", "provider_configuration_error",
    "provider_rejected", "provider_temporary_error", "provider_error", "repair_failed",
})


def fallback_language(bot_languages="", latest_text=""):
    configured = [item.strip().casefold() for item in re.split(r"[,;\n/|]+", str(bot_languages or "")) if item.strip()]
    configured = [_LANGUAGE_ALIASES.get(item.replace("_", "-"), item) for item in configured]
    if not configured:
        return "hindi" if re.search(r"[\u0900-\u097f]", str(latest_text)) else "english"
    # Script can narrow the configured languages, but cannot distinguish Hindi
    # from Marathi or German from English. Preserve authored order in those
    # cases instead of treating a shared alphabet as a language classifier.
    scripts = {
        "hindi": r"[\u0900-\u097f]", "marathi": r"[\u0900-\u097f]",
        "punjabi": r"[\u0a00-\u0a7f]", "kannada": r"[\u0c80-\u0cff]",
    }
    for language in configured:
        if language in scripts and re.search(scripts[language], str(latest_text)):
            return language
    return next((item for item in configured if item in _MESSAGES), "english")


def fallback_message(*, kind="technical", bot_languages="", latest_text="", question_type=""):
    if kind == "unverified" and question_type in {"pricing", "policy"}:
        kind = question_type
    messages = _MESSAGES[fallback_language(bot_languages, latest_text)]
    return messages.get(kind, messages["technical"])


def is_technical_fallback(message):
    normalized = " ".join(str(message or "").casefold().split())
    return any(" ".join(messages["technical"].casefold().split()) in normalized
               for messages in _MESSAGES.values())


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
