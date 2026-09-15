from __future__ import annotations

from functools import wraps
from uuid import UUID


_INSTALLED = False


def _persistent_lead_id(context) -> str | None:
    lead = getattr(context, "lead", None)
    if not isinstance(lead, dict):
        return None
    value = str(lead.get("id") or "").strip()
    if not value:
        return None
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None
    return value


def install_canonical_architecture_compat() -> None:
    """Keep pure/synthetic AI contexts database-free.

    Production AI contexts always contain the persisted UUID of a CRM Lead, so
    canonical state reconciliation may resolve that record. Unit tests, policy
    previews and other pure context builders intentionally use synthetic IDs such
    as ``lead-1``. Those contexts must remain side-effect free and must not query
    the CRM merely because the canonical response-input wrapper is installed.

    This guard unwraps only the reconciliation decorator for non-persistent
    contexts. It does not change any production decision, state, retrieval or
    execution behavior.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService

    canonical_build_input = EngagementService._build_input
    pure_build_input = getattr(canonical_build_input, "__wrapped__", None)
    if pure_build_input is None:
        _INSTALLED = True
        return

    @wraps(canonical_build_input)
    def persistent_context_only(self, *, context, **kwargs):
        if _persistent_lead_id(context) is None:
            return pure_build_input(self, context=context, **kwargs)
        return canonical_build_input(self, context=context, **kwargs)

    EngagementService._build_input = persistent_context_only
    _INSTALLED = True
