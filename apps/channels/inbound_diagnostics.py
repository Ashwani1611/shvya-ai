"""Bounded, display-only provider metadata for unreadable inbound events."""

import re
import uuid

from apps.integrations.diagnostic_auth import sanitize_text
from services.channels.whatsapp_error_service import extract_meta_error


_PROVIDER_TYPES = frozenset("text image audio video document sticker contacts location button interactive order system unknown unsupported reaction".split())
_UNSUPPORTED_TYPES = _PROVIDER_TYPES | frozenset(
    "edit revoke errors gif group_invite hsm keep_in_chat link_preview list media_placeholder pin "
    "poll_creation poll_update product scheduled_event_creation scheduled_event_edit status_mention "
    "status_reply video_note view_once".split()
)
_CODE = re.compile(r"[0-9]{1,6}")
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)\+?\d(?:[\s().-]*\d){7,}(?!\w)")
_BEARER = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"\b(?:(?:http|proxy)[_-])?authorization\b[\"']?\s*[:=].*",
    re.IGNORECASE | re.DOTALL,
)
_COOKIE_HEADER = re.compile(r"\b(?:set[-_])?cookie\b[\"']?\s*[:=].*", re.IGNORECASE | re.DOTALL)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|credential|"
    r"authorization|cookie|session|password|client[_-]?secret)\b[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", re.IGNORECASE,
)


def _safe_text(value, *, limit):
    if not isinstance(value, str):
        return ""
    # Never cut an input inside a secret/contact before redaction. Oversized
    # provider text is omitted rather than risking a partially exposed value.
    if len(value) > 4096:
        return "[provider text omitted: too long]"
    text = _AUTH_HEADER.sub("[secret omitted]", value)
    text = _COOKIE_HEADER.sub("[secret omitted]", text)
    text = _SECRET_ASSIGNMENT.sub("[secret omitted]", text)
    text = _BEARER.sub("Bearer [secret omitted]", text)
    text = sanitize_text(text, limit=4096)
    text = _EMAIL.sub("[contact omitted]", text)
    text = _URL.sub("[link omitted]", text)
    text = _PHONE.sub("[contact omitted]", text)
    return sanitize_text(text, limit=limit - 1)


def _safe_type(value, *, allowed):
    if isinstance(value, str) and len(value) <= 40 and value.lower() in allowed:
        return value.lower()
    return ""


def inbound_event_details(message):
    """Project an allowlist without exposing payloads, contacts or body text.

    Call after the existing text/button recovery in the scoped inbox view.
    This helper neither changes persisted messages nor infers why the provider
    did not include readable content.
    """
    if message.direction != "inbound" or str(message.body or "").strip():
        return None

    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    unsupported = payload.get("unsupported")
    unsupported = unsupported if isinstance(unsupported, dict) else {}
    errors = payload.get("errors")
    errors = errors if isinstance(errors, list) else []
    safe_errors = []
    for error in errors[:3]:
        if not isinstance(error, dict):
            continue
        # Never stringify nested malformed values: a title/details object can
        # contain unrelated payload keys or contact/credential material.
        scalar_error = {key: _safe_text(error[key], limit=120 if key in {"title", "type"} else 400)
                        for key in ("title", "type", "message", "details")
                        if isinstance(error.get(key), str)}
        error_data = error.get("error_data")
        if isinstance(error_data, dict) and isinstance(error_data.get("details"), str):
            scalar_error["error_data"] = {"details": _safe_text(error_data["details"], limit=400)}
        extracted = extract_meta_error(raw_payload={"errors": [scalar_error]})
        raw_code = error.get("code")
        code = (str(raw_code) if type(raw_code) is int and 0 <= raw_code <= 999999
                else raw_code if isinstance(raw_code, str) and len(raw_code) <= 6 else "")
        safe_error = {
            "code": code if _CODE.fullmatch(code) else "",
            "title": extracted["meta_title"],
            "detail": extracted["details"] or extracted["message"],
        }
        if any(safe_error.values()):
            safe_errors.append(safe_error)

    try:
        message_id = str(uuid.UUID(str(message.pk)))
    except (ValueError, TypeError, AttributeError):
        message_id = ""
    return {
        "message_id": message_id,
        "provider_type": _safe_type(payload.get("type"), allowed=_PROVIDER_TYPES) or "Unavailable",
        "unsupported_type": _safe_type(unsupported.get("type"), allowed=_UNSUPPORTED_TYPES),
        "errors": safe_errors,
    }
