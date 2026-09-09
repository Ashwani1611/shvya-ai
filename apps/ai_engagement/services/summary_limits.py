"""Shared hard limits for CRM summaries (Unicode characters, not tokens)."""

MAX_SUMMARY_CHARS = 500
MAX_UPDATE_CHARS = 150


def compact(text, limit=MAX_SUMMARY_CHARS):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def merge_summary(existing, addition, limit=MAX_SUMMARY_CHARS):
    existing = compact(existing, limit)
    addition = compact(addition, MAX_UPDATE_CHARS if existing else limit)
    if not addition or addition.casefold() in existing.casefold():
        return existing
    if not existing:
        return addition
    return compact(existing, limit - len(addition) - 1) + " " + addition

