from __future__ import annotations

"""Deprecated qualification CRM bridge.

Qualification attribute mapping and completion-stage execution are no longer
implemented here. The authoritative path is
``qualification_execution_contract`` + ``qualification_execution_policy_guard``.

A small reminder-time compatibility symbol remains because normal-conversation
runtime modules historically imported it from this module. It delegates to the
standalone reminder parser and has no qualification authority.
"""

from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at


_INSTALLED = False


def _parse_grounded_due_at(text: str) -> str | None:
    return parse_grounded_due_at(text)


def install_qualification_crm_action_runtime() -> None:
    """Compatibility no-op; qualification execution moved to the new contract."""
    global _INSTALLED
    _INSTALLED = True
