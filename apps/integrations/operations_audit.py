"""Viewer-safe presentation helpers for SHVYA Operations audit metadata."""

from __future__ import annotations


_ORGANIZATION_CONTEXT_REASON = {
    "select_organization_context": "SHVYA Support context started for this organization.",
    "clear_organization_context": "SHVYA Support context ended for this organization.",
    "support_context_force_end": "SHVYA Support context was ended for this organization.",
}


def organization_visible_audit_reason(event) -> str:
    """Return customer-safe reason text without weakening the immutable audit row."""

    tool_name = str(getattr(event, "tool_name", "") or "")
    if tool_name in _ORGANIZATION_CONTEXT_REASON:
        return _ORGANIZATION_CONTEXT_REASON[tool_name]
    return str(getattr(event, "reason", "") or "")
