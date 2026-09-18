from __future__ import annotations

import re
from typing import Any, Mapping

from apps.ai_engagement.services.trace_sanitizer import redact_text


SAFE_CONFIDENTIALITY_REPLY = (
    "I can’t share private or internal system information. "
    "I can still help with your enquiry or ask the team to assist."
)

_SENSITIVE_FIELD_PARTS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "cookie",
        "credential",
        "cvv",
        "databaseurl",
        "oauth",
        "otp",
        "passcode",
        "passwd",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "session",
        "signingkey",
        "smtppassword",
        "token",
        "webhooksecret",
    }
)

_UUID_RE = re.compile(
    r"(?<![A-Fa-f0-9])"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[1-5][A-Fa-f0-9]{3}-"
    r"[89ABab][A-Fa-f0-9]{3}-[A-Fa-f0-9]{12}"
    r"(?![A-Fa-f0-9])"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----",
    flags=re.IGNORECASE,
)
_DATABASE_URL_RE = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s]+",
    flags=re.IGNORECASE,
)
_DIRECT_SECRET_RE = re.compile(
    r"\b(?:otp|cvv|security\s+pin|account\s+pin)\s*(?:is|:|=)\s*[A-Za-z0-9-]{3,}",
    flags=re.IGNORECASE,
)
_INTERNAL_SCHEMA_RE = re.compile(
    r"\b(?:crm_actions|qualification_updates|next_requirement_id|"
    r"source_message_id|organization_id|pipeline_id|stage_id|backend_revision|"
    r"flow_version|runtime_policy|reconciled_state|shvya_ai_processing)\b",
    flags=re.IGNORECASE,
)
_INSTRUCTION_DISCLOSURE_RE = re.compile(
    r"\b(?:system prompt|developer message|hidden instructions?|"
    r"internal instructions?|chain[- ]of[- ]thought|hidden reasoning|"
    r"internal reasoning)\b",
    flags=re.IGNORECASE,
)
_PROMPT_SECTION_RE = re.compile(
    r"\b(?:instruction precedence|backend qualification turn|"
    r"qualification state invariants|application-controlled customer engagement mode|"
    r"customer-facing safety|output discipline)\b",
    flags=re.IGNORECASE,
)
_INTERNAL_ROUTE_RE = re.compile(
    r"(?:\b(?:internal|crm|lead)\b.{0,45}\b(?:pipeline|stage)\b|"
    r"\b(?:pipeline|stage)\b.{0,45}\b(?:internal|crm|lead|id|routing)\b|"
    r"\b(?:moved|moving|routed|routing)\s+you\s+(?:to|into)\b)",
    flags=re.IGNORECASE,
)
_PRIVATE_CRM_DISCLOSURE_RE = re.compile(
    r"(?:\b(?:internal|private|crm)\b.{0,36}"
    r"\b(?:attribute|contact|metadata|note|record)\b|"
    r"\b(?:lead|customer)\s+(?:attribute|metadata|note)\b|"
    r"\bstored\s+(?:crm\s+)?(?:value|phone\s+number|email\s+address)\b)",
    flags=re.IGNORECASE,
)
_INTERNAL_IMPLEMENTATION_RE = re.compile(
    r"(?:\bTraceback \(most recent call last\):|"
    r"\bapps[./\\]ai_engagement[./\\]|"
    r"\bservices[./\\]channels[./\\]|"
    r"\b(?:django|celery)\b.{0,24}\b(?:setting|exception|traceback)\b)",
    flags=re.IGNORECASE,
)


def _compact_identifier(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def is_sensitive_field_name(value: Any) -> bool:
    """Return whether a field name represents secret/credential material."""

    compact = _compact_identifier(value)
    if not compact:
        return False
    return any(
        compact == part or compact.startswith(part) or compact.endswith(part)
        for part in _SENSITIVE_FIELD_PARTS
    )


def is_sensitive_attribute_definition(definition: Mapping[str, Any] | Any) -> bool:
    if not isinstance(definition, Mapping):
        return False
    return any(
        is_sensitive_field_name(definition.get(key))
        for key in ("key", "name")
    )


def safe_attribute_values(value: Any) -> dict[str, Any]:
    """Expose only customer-engagement-safe CRM attributes to the model."""

    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if not str(key).startswith("_") and not is_sensitive_field_name(key)
    }


def customer_message_violation(message: Any) -> str | None:
    """Classify internal/confidential material in customer-facing text.

    This is deliberately fail-closed. It returns only a stable reason code and
    never returns, logs, or embeds the detected secret value.
    """

    text = str(message or "").strip()
    if not text:
        return None
    if redact_text(text) != text:
        return "credential_material"
    if (
        _PRIVATE_KEY_RE.search(text)
        or _DATABASE_URL_RE.search(text)
        or _DIRECT_SECRET_RE.search(text)
    ):
        return "credential_material"
    if _UUID_RE.search(text):
        return "internal_identifier"
    if _INTERNAL_SCHEMA_RE.search(text):
        return "internal_schema"
    if _INSTRUCTION_DISCLOSURE_RE.search(text) or _PROMPT_SECTION_RE.search(text):
        return "instruction_disclosure"
    if _INTERNAL_ROUTE_RE.search(text):
        return "internal_routing"
    if _PRIVATE_CRM_DISCLOSURE_RE.search(text):
        return "private_crm_data"
    if _INTERNAL_IMPLEMENTATION_RE.search(text):
        return "implementation_detail"
    return None


def protect_customer_message(message: Any) -> tuple[str, str | None]:
    text = str(message or "").strip()
    violation = customer_message_violation(text)
    if violation:
        return SAFE_CONFIDENTIALITY_REPLY, violation
    return text, None
