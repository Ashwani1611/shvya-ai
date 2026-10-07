"""Pure projections for Sandbox's existing, language-only final response pass.

These helpers never authorize actions, query the database, or send messages.
Only the results returned by preview_effects establish applied preview effects.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import re
from typing import Any, Iterator


_PREVIEW_ACTION_TYPES = {
    "attribute_updates": "attribute_updates",
    "reminder": "create_reminder",
    "stage_transition": "pipeline_transition",
}

_OWN_ACTION = r"\b(?:i|we|they|our\s+team|the\s+team)(?:\s+(?:will|shall|am|are|have|has)|['’](?:ll|m|re|ve))\s+(?:now\s+)?(?:proceed\s+to\s+)?"
_FILE_SEND = r"(?:send|sending|sent|share|sharing|shared|attach|attaching|attached)\s+(?:you\s+)?(?:(?:the|a|your|our|product|requested)\s+){0,3}(?:brochure|catalog(?:ue)?|pdf|file|document|guide)\b"
_CALL_ACTION = r"(?:(?:schedule|scheduling|scheduled|book|booking|booked|confirm|confirming|confirmed|create|creating|created|arrange|arranging|arranged|set\s+up)\s+(?:for\s+)?(?:(?:the|a|your|requested|follow-up)\s+){0,3}(?:call|callback|reminder|appointment|booking|demo|trial|visit|session)\b(?!\s+(?:platform|software|system|tool|service|feature))|(?:call|contact|connect)\s+you\b|reach\s+out\s+to\s+you\b|pass\s+(?:your|the|this)\s+(?:request|details)\s+to\b|pass\s+it\s+(?:along|on)\b|coordinate\s+with\s+(?:(?:our|the)\s+)?team\s+to\s+(?:schedule|arrange)\b|be\s+in\s+touch\b)"
_ACTION_ASSURANCE = re.compile(
    _OWN_ACTION + r"(?:" + _FILE_SEND + "|" + _CALL_ACTION + r")|"
    r"\b(?:i|we|our\s+team|the\s+team)\s+(?=(?:sent|shared|attached|scheduled|booked|confirmed|created|arranged)\b)(?:" + _FILE_SEND + "|" + _CALL_ACTION + r")|"
    r"\b(?:brochure|catalog(?:ue)?|pdf|file|document|guide|call|callback|reminder|appointment|booking|demo|trial|visit|session)\s+"
    r"(?:has|have|is|are|was|were)\s+(?:been\s+)?(?:now\s+)?(?:sent|shared|attached|scheduled|booked|confirmed|created)\b|"
    r"\byour\s+(?:trial|visit|session|appointment|booking|demo)\b[^.!?\n]{0,240}?"
    r"\b(?:has|have|is|are|was|were)\s+(?:been\s+)?(?:now\s+)?(?:successfully\s+)?(?:scheduled|booked|confirmed|reserved)\b|"
    r"^\s*(?![^.!?\n]*\?)you(?:\s+are|['’]re)\s+(?:now\s+)?all\s+set\s+for\s+"
    r"(?:(?:your|the|a|an|upcoming|free|paid)\s+){0,3}(?:trial|visit|session|appointment|booking|demo)\b|"
    r"\b(?:main|hum|ham|team)\s+(?:ab\s+)?(?:(?:aapko|apko)\s+)?(?:(?:brochure|file|document|guide)\s+)?(?:bhejunga|bhejenge|bhej\s+(?:raha|rahe)|"
    r"(?:call|reminder|file|brochure)\s+.{0,40}?kar\s+(?:diya|di|dunga|denge))\b|"
    r"(?:मैं|हम|टीम)\s*(?:अभी\s*)?(?:भेज(?:ूँगा|ेंगी|ेंगे)|.{0,40}?कर\s*(?:दिया|दूँगा|देंगे))", re.IGNORECASE,
)
_PREVIEW_WORD = re.compile(r"\b(?:preview|simulated|simulation|sandbox)\b|(?:प्रीव्यू|सिम्युलेट)", re.IGNORECASE)
_FILE_OBJECT = re.compile(r"\b(?:brochure|catalog(?:ue)?|pdf|file|document|guide)\b|(?:ब्रोशर|ब्रोशुर|फ़ाइल|फाइल|दस्तावेज़|दस्तावेज)", re.IGNORECASE)
_CALL_OBJECT = re.compile(r"\b(?:call|callback|handoff|human|appointment|booking|demo)\b|(?:कॉल|हैंडऑफ़)", re.IGNORECASE)


def enforce_preview_action_honesty(*, decision, events, files, requested_text="", allowed_languages=()):
    """Keep simulated outcomes honest even when the final model pass is skipped.

    Receipts come exclusively from this turn's preview result. This changes
    language only, and cannot select a file, schedule a call or mutate state.
    """
    if not decision.should_engage:
        return decision
    message = str(decision.message or "")
    sentences = re.split(r"(?<=[.!?।])\s+(?!\d)|\n+", message)
    retained, removed = [], False
    for sentence in sentences:
        # Keep a supported price/business clause when an unsupported promise
        # is appended to it. Currency/grouping commas never match this split.
        clauses = re.split(r";\s*|,\s*(?:and\s+)?(?=(?:i|we|our\s+team|the\s+team)\b)", sentence, flags=re.IGNORECASE)
        for clause in clauses:
            assurance = _ACTION_ASSURANCE.search(clause)
            negated_subject = assurance and re.search(r"\b(?:no|not|never)\s*$", clause[:assurance.start()], re.IGNORECASE)
            if assurance and not negated_subject:
                removed = True
            else:
                retained.append(clause)
    text = "\n".join(retained).strip() if removed else message
    preview_types = {
        item.get("type") for item in events or []
        if isinstance(item, dict) and item.get("status") == "preview"
    }
    file_id = preview_file_id(files)
    file_requested = bool(_FILE_OBJECT.search(requested_text))
    call_requested = bool(_CALL_OBJECT.search(requested_text))
    booking_requested = bool(re.search(r"\b(?:trial|visit|session|appointment|booking|demo)\b", requested_text, re.IGNORECASE))
    # Plain factual answers and qualification questions remain untouched.
    if not removed:
        return decision
    from apps.ai_engagement.services.intent_rules import canonical_language, detect_language
    language = canonical_language(detect_language(text or message or requested_text))
    configured = [canonical_language(item) for item in allowed_languages or ()]
    if configured and language not in configured:
        language = configured[0]
    if language in {"hi", "hindi"}:
        clauses = []
        if file_id is not None:
            clauses.append("दस्तावेज़ इस प्रीव्यू में उपलब्ध है; किसी ग्राहक को भेजा नहीं गया है।")
        elif removed and file_requested:
            clauses.append("इस प्रीव्यू में कोई दस्तावेज़ भेजा नहीं गया है।")
        if "reminder" in preview_types:
            clauses.append("फ़ॉलो-अप रिमाइंडर केवल प्रीव्यू में दिखाया गया है; वास्तविक कॉल तय नहीं हुई है।")
        if booking_requested:
            clauses.append("यह केवल सैंडबॉक्स प्रीव्यू है; वास्तविक ट्रायल, विज़िट या बुकिंग की पुष्टि नहीं हुई है।")
        elif call_requested and ("stage_transition" in preview_types or removed):
            clauses.append("यह केवल सैंडबॉक्स प्रीव्यू है; वास्तविक कॉल या हैंडऑफ़ की पुष्टि नहीं हुई है।")
    elif language == "hinglish":
        clauses = []
        if file_id is not None:
            clauses.append("Document is preview mein available hai; customer ko live send nahi hua hai.")
        elif removed and file_requested:
            clauses.append("Is preview mein koi document send nahi hua hai.")
        if "reminder" in preview_types:
            clauses.append("Follow-up reminder sirf preview mein hai; live call confirm nahi hui hai.")
        if booking_requested:
            clauses.append("Yeh sirf Sandbox preview hai; live trial, visit ya booking confirm nahi hui hai.")
        elif call_requested and ("stage_transition" in preview_types or removed):
            clauses.append("Yeh Sandbox preview hai; live call ya handoff confirm nahi hua hai.")
    elif language in {"en", "english"}:
        clauses = []
        if file_id is not None:
            clauses.append("The document is available in this preview; it has not been sent to a customer.")
        elif removed and file_requested:
            clauses.append("No document was shared in this preview.")
        if "reminder" in preview_types:
            clauses.append("The follow-up reminder is shown in this preview only; no live call is confirmed.")
        if booking_requested:
            clauses.append("This is a Sandbox preview; no live trial, visit or booking is confirmed.")
        elif call_requested and ("stage_transition" in preview_types or removed):
            clauses.append("This is a Sandbox preview; no live call or handoff is confirmed.")
    else:
        # The UI already displays preview facts. Do not inject English into
        # another configured language when no deterministic translation exists.
        clauses = []
    # Avoid repeating a truthful final explanation when the model already
    # distinguished the simulated outcome from a live action.
    described = [sentence for sentence in retained if _PREVIEW_WORD.search(sentence)]
    file_described = any(_FILE_OBJECT.search(sentence) for sentence in described)
    reminder_described = any(re.search(r"\breminder\b|रिमाइंडर", sentence, re.IGNORECASE) for sentence in described)
    if not removed and (file_id is None or file_described) and ("reminder" not in preview_types or reminder_described):
        return decision
    return replace(decision, message="\n".join(part for part in [text, *clauses] if part))


def needs_final_composition(*, decision, events, files) -> bool:
    """Also finalize attempted effects that were rejected or made no change."""
    # The validated qualification reply already asks the next question. Running
    # it again after projecting the answer exposes the following question as
    # active and lets the model reinterpret the same option letter against it.
    # Attribute-only progress needs no customer-facing execution claim.
    if (
        decision.should_engage and (decision.qualification_updates or events)
        and decision.next_requirement_id
        and not files and decision.file_document_id is None
        and all(item.get("type") == "attribute_updates" for item in decision.crm_actions or [])
        and all(item.get("type") == "attribute_updates" for item in events or [])
    ):
        return False
    return bool(
        decision.should_engage
        and (
            events or files or decision.crm_actions
            or decision.qualification_updates
            or decision.file_document_id is not None
        )
    )


def preview_file_id(files) -> int | None:
    """Return the one validated preview selection, never the model proposal."""
    if not isinstance(files, list) or len(files) != 1:
        return None
    item = files[0]
    value = item.get("id") if isinstance(item, dict) else None
    return value if type(value) is int and value > 0 else None


def resolved_preview_actions(*, visitor, decision, events, files, source_message_id) -> dict[str, Any]:
    """Project this turn's preview results without promoting them to CRM receipts.

    A missing event can also mean a no-op; report not_applied, not failed.
    Attribute values, file URLs, and raw provider errors are deliberately absent.
    Existing safe AIContext fields carry the resulting values and reminder.
    """
    action_types = list(dict.fromkeys(
        _PREVIEW_ACTION_TYPES[event["type"]]
        for event in events or []
        if isinstance(event, dict) and event.get("status") == "preview"
        and event.get("type") in _PREVIEW_ACTION_TYPES
    ))
    proposed_types = list(dict.fromkeys(
        action["type"] for action in decision.crm_actions or []
        if isinstance(action, dict) and action.get("type") in _PREVIEW_ACTION_TYPES.values()
    ))
    result = {
        "source_message_id": str(source_message_id or ""),
        "execution_mode": "sandbox_preview",
        "action_types": action_types,
        "action_outcomes": [
            {"type": name, "status": "preview" if name in action_types else "not_applied"}
            for name in dict.fromkeys([*action_types, *proposed_types])
        ],
        "stage": {
            "id": str(getattr(visitor, "stage_id", "") or ""),
            "name": str(getattr(getattr(visitor, "stage", None), "name", "") or ""),
        },
    }
    document_id = preview_file_id(files)
    if document_id is not None:
        action_types.append("file_share")
        result["action_outcomes"].append({"type": "file_share", "status": "preview"})
        result["file_share"] = {
            "status": "preview",
            "document_id": document_id,
            "document_name": str(files[0].get("name") or ""),
        }
    elif decision.file_document_id is not None:
        result["action_outcomes"].append({"type": "file_share", "status": "not_applied"})
        result["file_share"] = {"status": "not_previewed", "document_id": None}
    return result


def language_only_decision(*, decision, files):
    """Successful and fallback final replies cannot introduce a second effect."""
    return replace(
        decision, crm_actions=[], qualification_updates=[],
        file_document_id=preview_file_id(files),
    )


@contextmanager
def preserve_preview_state(visitor) -> Iterator[None]:
    """Restore the in-memory visitor, including any fields added during language generation.

    Preserve ORM relationship identity; JSON-like session state gets deep copies.
    This is not a database transaction or protection against an ORM save: the
    caller must remain the Sandbox service with its existing no-write boundaries.
    """
    saved = {
        key: deepcopy(value) if isinstance(value, (dict, list, tuple, set)) else value
        for key, value in vars(visitor).items()
    }
    try:
        yield
    finally:
        vars(visitor).clear()
        vars(visitor).update(saved)
