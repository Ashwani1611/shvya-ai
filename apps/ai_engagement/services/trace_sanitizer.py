from __future__ import annotations

from collections.abc import Mapping, Sequence
import re

MAX_STRING = 1200
MAX_LIST = 50
MAX_DICT = 80
MAX_TOTAL_CHARS = 32000

_SECRET_TOKENS = {
    "access_token", "authorization", "authorization_header", "api_key", "apikey",
    "openai_api_key", "password", "passwd", "secret", "webhook_secret", "cookie",
    "cookies", "session_secret", "database_url", "database_password", "private_key",
    "signing_key", "smtp_password", "smtp_credentials", "refresh_token", "client_secret",
}


def _sensitive_key(key: object) -> bool:
    normalized = str(key or "").strip().casefold().replace("-", "_")
    if normalized in _SECRET_TOKENS:
        return True
    return any(
        token in normalized
        for token in (
            "password", "access_token", "authorization", "private_key",
            "client_secret", "webhook_secret", "session_secret", "smtp_credential",
        )
    )


def redact_text(value) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~+\-/=]+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|smtp[_-]?password)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]", text)
    return text


def _take_text(value: object, budget: list[int], limit: int = MAX_STRING) -> str:
    if budget[0] <= 0:
        return "[truncated]"
    text = redact_text(value)[: min(limit, budget[0])]
    budget[0] -= len(text)
    return text


def _sanitize(value, *, depth: int, budget: list[int]):
    if budget[0] <= 0:
        return "[truncated]"
    if depth > 6:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _take_text(value, budget)
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DICT or budget[0] <= 0:
                result["_truncated"] = True
                break
            safe_key = _take_text(key, budget, 120)
            result[safe_key] = (
                "[redacted]"
                if _sensitive_key(key)
                else _sanitize(item, depth=depth + 1, budget=budget)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _sanitize(item, depth=depth + 1, budget=budget)
            for item in list(value)[:MAX_LIST]
            if budget[0] > 0
        ]
    return _take_text(value, budget)


def sanitize(value):
    return _sanitize(value, depth=0, budget=[MAX_TOTAL_CHARS])


def preview(value, *, limit: int = 1200) -> str:
    return " ".join(redact_text(value).split())[: max(0, min(limit, 8000))]


def safe_error_message(value) -> str:
    return preview(value, limit=500)
