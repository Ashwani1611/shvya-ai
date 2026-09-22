"""Internal authorization context for one approved configuration plan.

Only apply/rollback configuration-plan handlers enter this context. Public MCP
schemas never expose these values; ordinary Operations writes still require
their own dry-run approval receipts.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar


_ACTIVE_PLAN = ContextVar("shvya_operations_configuration_plan", default=None)


@contextmanager
def configuration_plan_context(*, actor_id, organization_id, plan_id, tools):
    token = _ACTIVE_PLAN.set(
        {
            "actor_id": str(actor_id),
            "organization_id": str(organization_id),
            "plan_id": str(plan_id),
            "tools": frozenset(str(item) for item in tools),
        }
    )
    try:
        yield
    finally:
        _ACTIVE_PLAN.reset(token)


def configuration_plan_authorizes(*, identity, organization, tool_name):
    context = _ACTIVE_PLAN.get()
    if not isinstance(context, dict):
        return False
    return (
        context.get("actor_id") == str(identity.actor.id)
        and context.get("organization_id") == str(organization.id)
        and str(tool_name) in context.get("tools", ())
    )
