"""Conservative redaction for operational metadata, never payload capture."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from itertools import islice
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

REDACTED = "[redacted]"
MAX_METADATA_BYTES = 12_000
MAX_DEPTH = 4
MAX_ITEMS = 40
MAX_NODES = 160

# Allow diagnostic counts while excluding content and all credential material.
_SAFE_KEYS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
    "reasoning_tokens", "token_count", "token_usage", "token_limit",
    "prompt_tokens", "completion_tokens", "message_count", "content_type",
    "session_state", "session_status", "prompt_version", "body_size_bytes",
    "message_id", "message_status", "message_type", "event_name",
})
_PRIVATE_KEY = re.compile(
    r"authorization|authentication|cookie|password|passwd|secret|credential|"
    r"signature|api.?key|access.?token|refresh.?token|bearer|private.?key|"
    r"(^|_)(token|tokens|key|body|payload|headers?|conversation|prompt|content|"
    r"session|qr|text|message|phone|mobile|email|address|name)(_|$)", re.I,
)
_SECRET_PAIR = re.compile(
    r"(?i)([\"']?(?:authorization|password|passwd|secret|client_secret|"
    r"access_token|refresh_token|api[_-]?key|token|cookie|signature|"
    r"session[_-]?(?:id|key)|x-hub-signature-256)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)",
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/%=-]+", re.I)
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|EAA[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d ()-]{8,}\d(?![\w-])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_endpoint(value) -> str:
    """Keep a route/host; drop query, userinfo and credential path segments."""
    if not isinstance(value, (str, bytes)):
        return ""
    raw = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    raw = _CONTROL.sub("", raw[:4096])
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        segments = parts.path.split("/")
        safe_segments = []
        previous = ""
        for segment in segments:
            decoded = unquote(segment)
            # URL-based webhook tokens are credentials even when they are UUIDs.
            private_segment = previous in {"token", "tokens", "secret", "key", "auth", "session", "sessions"}
            private_segment |= bool(re.search(r"(?i)(?:token|secret|api[_-]?key)=", decoded))
            private_segment |= previous in {"justdial", "indiamart"} and bool(decoded) and decoded not in {
                "webhook", "ingest", "setup", "settings", "connect", "disconnect", "status", "test",
            } and not decoded.startswith("<")
            private_segment |= bool(re.fullmatch(r"[A-Za-z0-9_-]{24,}", decoded))
            private_segment |= bool(_KNOWN_TOKEN.search(decoded))
            private_segment |= bool(re.fullmatch(r"\+?\d{9,15}", decoded))
            private_segment |= "@" in decoded
            safe_segments.append(REDACTED if private_segment else segment)
            previous = decoded.lower()
        path = "/".join(safe_segments)
        return urlunsplit((parts.scheme, host, path, "", ""))[:500]
    except (ValueError, TypeError):
        return "[invalid endpoint]"


def _sanitize_text(value, *, max_length=500, mask_personal=True) -> str:
    """Redact recognizable secrets/PII without stringifying arbitrary objects."""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool, UUID, date, datetime)):
        return "[unsupported]"
    text = _CONTROL.sub("", str(value)[:8192])
    text = re.sub(r"-----BEGIN[^-]*PRIVATE KEY-----.*", REDACTED, text, flags=re.S)
    text = _URL.sub(lambda match: sanitize_endpoint(match.group()), text)
    text = _BEARER.sub(REDACTED, text)
    text = _SECRET_PAIR.sub(lambda match: match.group(1) + REDACTED, text)
    text = _KNOWN_TOKEN.sub(REDACTED, text)
    if mask_personal:
        text = _EMAIL.sub(REDACTED, text)
        text = _PHONE.sub(REDACTED, text)
    return text[:max(0, min(int(max_length), 8192))]


def sanitize_text(value, *, max_length=500) -> str:
    return _sanitize_text(value, max_length=max_length)


def sanitize_identifier(value, *, max_length=160) -> str:
    """Identifiers may be long decimal provider IDs; still redact credentials."""
    text = _sanitize_text(value, max_length=max_length, mask_personal=False)
    return REDACTED if "@" in text else text


def sanitize_metadata(value):
    """Return JSON-safe, depth/count/byte-bounded diagnostic metadata.

    Arbitrary objects and exception instances are deliberately not repr'd.
    Raw headers, bodies, messages, prompts and session/QR data are excluded.
    """
    remaining = [MAX_NODES]

    def clean(item, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > MAX_DEPTH:
            return "[truncated]"
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int):
            return item if abs(item) < 10**20 else "[number too large]"
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, (str, UUID, date, datetime)):
            return sanitize_text(item, max_length=500)
        if isinstance(item, dict):
            output = {}
            for key, entry in islice(item.items(), MAX_ITEMS):
                if not isinstance(key, str):
                    continue
                safe_key = sanitize_text(key, max_length=80)
                normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
                normalized = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")
                if normalized not in _SAFE_KEYS and _PRIVATE_KEY.search(normalized):
                    output[safe_key] = REDACTED
                elif normalized.endswith("_id") and isinstance(entry, (str, int, UUID)):
                    output[safe_key] = sanitize_identifier(entry)
                else:
                    output[safe_key] = clean(entry, depth + 1)
                if remaining[0] <= 0:
                    break
            return output
        if isinstance(item, (list, tuple)):
            return [clean(entry, depth + 1) for entry in islice(item, min(MAX_ITEMS, max(0, remaining[0])))]
        return "[unsupported]"

    result = clean({} if value is None else value, 0)
    encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        return {"truncated": True, "reason": "metadata_size_limit"}
    return result
