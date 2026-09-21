"""Approval fingerprint helpers for SHVYA Operations MCP."""

from __future__ import annotations

from apps.integrations.diagnostic_auth import request_fingerprint

_APPROVAL_CONTROL_FIELDS = {
    "dry_run",
    "approved",
    "approval_event_id",
}


def approval_payload(arguments):
    """Return the mutation proposal independent of execution-control flags."""

    if not isinstance(arguments, dict):
        return {}
    return {
        key: value
        for key, value in arguments.items()
        if key not in _APPROVAL_CONTROL_FIELDS
    }


def approval_fingerprint(arguments) -> str:
    """Stable fingerprint shared by dry-run and approved execution calls."""

    return request_fingerprint(approval_payload(arguments))
