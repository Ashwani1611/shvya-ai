from __future__ import annotations

"""Deprecated legacy qualification action projection.

The old implementation semantically projected qualification answers into CRM
attributes and selected a magic ``Qualified`` stage. Both behaviors conflict with
the organization-configured qualification execution contract and are retired.
"""

_INSTALLED = False


def install_crm_action_projection_fix() -> None:
    """Compatibility no-op; exact projection is owned by the new contract."""
    global _INSTALLED
    _INSTALLED = True
