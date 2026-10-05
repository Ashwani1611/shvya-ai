"""Bounded locale support for explicit reminder requests and supplied times.

This is not a general translation/date inference layer. Unsupported wording,
ambiguous day names (notably Punjabi ``ਕੱਲ੍ਹ``), and bare twelve-hour clocks
stay on the clarification path. Returned text is used only for time parsing.
"""
from __future__ import annotations

import re
import unicodedata


_BLOCKED = re.compile(
    r"\b(?:nicht|nie|kein(?:e[nsrm]?)?|wenn|falls)\b|"
    r"(?:नको|नका|नाही|(?<!\S)जर\s|ਨਹੀਂ|ਨਹੀ|(?<!\S)ਨਾ(?:\s|$)|(?<!\S)ਜੇ\s|ਬੰਦ|"
    r"ಬೇಡ|ಬೇಡಿ|ಬೇಡಿರಿ|ಬಾರದ|ಇಲ್ಲ|ಒಂದು\s+ವೇಳೆ)", re.I,
)
_CALLBACK = re.compile(
    r"\b(?:ruf|rufen\s+sie)\s+mich\b.{0,100}\ban\b|"
    r"(?:मला.{0,100}(?:फोन|कॉल)\s*करा|"
    r"ਮੈਨੂੰ.{0,100}(?:ਫੋਨ|ਫ਼ੋਨ|ਕਾਲ)\s*ਕਰੋ|"
    r"ನನಗೆ.{0,100}(?:ಕರೆ|ಫೋನ್)\s*ಮಾಡಿ)", re.I,
)
_FOLLOWUP = re.compile(
    r"\b(?:erinnere|erinnern\s+sie)\s+mich\b|"
    r"(?:मला.{0,100}आठवण\s*करून\s*द्या|"
    r"ਮੈਨੂੰ.{0,100}ਯਾਦ\s*(?:ਕਰਾਓ|ਕਰਵਾਓ|ਦਿਵਾਓ)|"
    r"ನನಗೆ.{0,100}ನೆನಪಿಸಿ)", re.I,
)
_ALTERNATIVES = re.compile(r"\boder\b|(?:किंवा|ਅਥਵਾ|ਜਾਂ|ಅಥವಾ)", re.I)
_PAST = re.compile(r"\b(?:gestern|vorgestern)\b|(?:काल|ਕੱਲ੍ਹ|ನಿನ್ನೆ)", re.I)
_CONDITIONAL = re.compile(r"\b(?:wenn|falls)\b|(?<!\S)(?:जर|ਜੇ)(?!\S)|ಒಂದು\s+ವೇಳೆ", re.I)
_GERMAN_MORNING = re.compile(r"\b(?:am|jeden|guten|der|den|ein(?:en)?|dies(?:en|er))\s+morgen\b", re.I)

_DAYS = {
    "übermorgen": "day after tomorrow", "morgen": "tomorrow", "heute": "today",
    "उद्या": "tomorrow", "आज": "today", "ਭਲਕੇ": "tomorrow", "ਅੱਜ": "today",
    "ನಾಳೆ": "tomorrow", "ಇಂದು": "today",
}
_RELATIVES = (
    (r"\bin\s+(\d{1,3})\s+(minuten?|stunden?|tag(?:e|en)?|wochen?)\b",
     {"min": "minutes", "stund": "hours", "tag": "days", "woch": "weeks"}),
    (r"(\d{1,3})\s*(मिनिट(?:ांनंतर|ा?नंतर|ांनी)|तास(?:ांनंतर|ांनी)|दिवस(?:ांनंतर|ां?नी)|आठवड्यांनंतर)",
     {"मिनिट": "minutes", "तास": "hours", "दिवस": "days", "आठवड": "weeks"}),
    (r"(\d{1,3})\s*(ਮਿੰਟ(?:ਾਂ)?|ਘੰਟੇ|ਘੰਟਿਆਂ|ਦਿਨ(?:ਾਂ)?|ਹਫ਼ਤੇ|ਹਫ਼ਤਿਆਂ)\s+ਬਾਅਦ",
     {"ਮਿੰਟ": "minutes", "ਘੰਟ": "hours", "ਦਿਨ": "days", "ਹਫ਼ਤ": "weeks"}),
    (r"(\d{1,3})\s*(ನಿಮಿಷ(?:ಗಳ)?|ಗಂಟೆ(?:ಗಳ)?|ದಿನ(?:ಗಳ)?|ವಾರ(?:ಗಳ)?)\s*ನಂತರ",
     {"ನಿಮಿಷ": "minutes", "ಗಂಟೆ": "hours", "ದಿನ": "days", "ವಾರ": "weeks"}),
)
_DAY_PARTS = {
    "morgens": "am", "vormittags": "am", "nachmittags": "pm", "abends": "pm",
    "सकाळी": "am", "दुपारी": "pm", "संध्याकाळी": "pm", "रात्री": "pm",
    "ਸਵੇਰੇ": "am", "ਦੁਪਹਿਰੇ": "pm", "ਦੁਪਹਿਰ": "pm", "ਸ਼ਾਮ": "pm", "ਸ਼ਾਮ": "pm", "ਰਾਤ": "pm",
    "ಬೆಳಿಗ್ಗೆ": "am", "ಮಧ್ಯಾಹ್ನ": "pm", "ಸಂಜೆ": "pm", "ರಾತ್ರಿ": "pm",
}


def localized_request_blocked(text):
    return bool(_BLOCKED.search(str(text or "")))


def localized_request_conditional(text):
    return bool(_CONDITIONAL.search(text))


def localized_request_kind(text):
    if localized_request_blocked(text):
        return None
    if _CALLBACK.search(text):
        return "callback"
    if _FOLLOWUP.search(text):
        return "later follow-up"
    return None


def localized_time_ambiguous(text):
    return bool(_ALTERNATIVES.search(text) or _PAST.search(text) or _GERMAN_MORNING.search(text))


def normalize_localized_time(text):
    """Map finite, explicit temporal expressions to the existing safe parser."""
    text = unicodedata.normalize("NFC", str(text or "")).casefold()
    text = "".join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in text)
    # Whitespace boundaries preserve combining marks in Indic words, for which
    # regex \b is not a reliable lexical boundary.
    for name, translated in _DAYS.items():
        text = re.sub(rf"(?<!\S){re.escape(name)}(?=$|[\s,.!?;])", translated, text)
    for pattern, units in _RELATIVES:
        def replace(match):
            unit = next(value for prefix, value in units.items() if match[2].startswith(prefix))
            return f"in {match[1]} {unit}"
        text = re.sub(pattern, replace, text, flags=re.I)
    for part, ampm in _DAY_PARTS.items():
        pattern = rf"(?<!\S){re.escape(part)}\s+(?:(?:um|ਨੂੰ)\s+)?(1[0-2]|0?[1-9])(?::([0-5]\d))?(?!\d)"
        def clock(match):
            # Twelve without explicit AM/PM is ambiguous across day-part idioms.
            hour = int(match[1])
            if hour == 12 or (part in {"रात्री", "ਰਾਤ", "ರಾತ್ರಿ"} and hour < 6):
                return match[0]
            return f"{match[1]}:{match[2] or '00'} {ampm}"
        text = re.sub(pattern, clock, text, flags=re.I)
    # German explicit 24h notation. Bare 1-12 Uhr still needs AM/PM/day part.
    text = re.sub(r"\b(?:um\s+)?(1[3-9]|2[0-3])\s+uhr\b", r"\1:00", text)
    return text
