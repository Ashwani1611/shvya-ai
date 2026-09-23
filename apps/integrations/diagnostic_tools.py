# ruff: noqa: F401
"""Compatibility facade for SHVYA diagnostic tools.

Focused implementations live under apps.integrations.operations.diagnostics.
Existing tool names, helper imports, tenant-safe behavior, and dispatch contracts
remain available from this module.
"""

from apps.integrations.operations.diagnostics import common as _common
from apps.integrations.operations.diagnostics import leads as _leads
from apps.integrations.operations.diagnostics import messaging as _messaging
from apps.integrations.operations.diagnostics import runtime as _runtime

_safe_diagnostic_code = _common._safe_diagnostic_code
_safe_ai_markers = _common._safe_ai_markers
_safe_hosted_job = _common._safe_hosted_job
DiagnosticToolError = _common.DiagnosticToolError
_iso = _common._iso
_tenant_safe_leads = _common._tenant_safe_leads
_tenant_safe_whatsapp_messages = _common._tenant_safe_whatsapp_messages
_tenant_safe_instagram_conversations = _common._tenant_safe_instagram_conversations
_tenant_safe_instagram_messages = _common._tenant_safe_instagram_messages
_tenant_safe_instagram_webhook_deliveries = _common._tenant_safe_instagram_webhook_deliveries
_tenant_safe_hosted_jobs = _common._tenant_safe_hosted_jobs
_tenant_safe_trigger_events = _common._tenant_safe_trigger_events
_tenant_safe_trigger_runs = _common._tenant_safe_trigger_runs
_tenant_safe_webhook_deliveries = _common._tenant_safe_webhook_deliveries
_lead_for_org = _common._lead_for_org
_safe_lead = _common._safe_lead
_safe_lead_attributes = _leads._safe_lead_attributes
_safe_wa_message = _messaging._safe_wa_message
_safe_instagram_message = _messaging._safe_instagram_message
_instagram_media_diagnostic_summary = _messaging._instagram_media_diagnostic_summary


_IMPLEMENTATIONS = (_common, _leads, _messaging, _runtime)
_ENTRYPOINTS = frozenset([
    "get_workspace_profile",
    "find_leads",
    "find_affected_leads",
    "get_lead_snapshot",
    "get_conversation",
    "trace_message",
    "get_ai_diagnostics",
    "get_integration_health",
    "get_workflow_trace",
    "get_recent_errors",
    "get_runtime_health"
])


def _sync_facade_overrides():
    """Propagate historical facade patch points into focused implementations."""
    facade = globals()
    for module in _IMPLEMENTATIONS:
        for name in tuple(module.__dict__):
            if name in _ENTRYPOINTS:
                continue
            if name in facade:
                setattr(module, name, facade[name])


def get_workspace_profile(*, organization, arguments):
    _sync_facade_overrides()
    return _leads.get_workspace_profile(organization=organization, arguments=arguments)


def find_leads(*, organization, arguments):
    _sync_facade_overrides()
    return _leads.find_leads(organization=organization, arguments=arguments)


def find_affected_leads(*, organization, arguments):
    _sync_facade_overrides()
    return _leads.find_affected_leads(organization=organization, arguments=arguments)


def get_lead_snapshot(*, organization, arguments):
    _sync_facade_overrides()
    return _leads.get_lead_snapshot(organization=organization, arguments=arguments)


def get_conversation(*, organization, arguments):
    _sync_facade_overrides()
    return _messaging.get_conversation(organization=organization, arguments=arguments)


def trace_message(*, organization, arguments):
    _sync_facade_overrides()
    return _messaging.trace_message(organization=organization, arguments=arguments)


def get_ai_diagnostics(*, organization, arguments):
    _sync_facade_overrides()
    return _messaging.get_ai_diagnostics(organization=organization, arguments=arguments)


def get_integration_health(*, organization, arguments):
    _sync_facade_overrides()
    return _messaging.get_integration_health(organization=organization, arguments=arguments)


def get_workflow_trace(*, organization, arguments):
    _sync_facade_overrides()
    return _runtime.get_workflow_trace(organization=organization, arguments=arguments)


def get_recent_errors(*, organization, arguments):
    _sync_facade_overrides()
    return _runtime.get_recent_errors(organization=organization, arguments=arguments)


def get_runtime_health(*, organization, arguments):
    _sync_facade_overrides()
    return _runtime.get_runtime_health(organization=organization, arguments=arguments)


TOOL_HANDLERS = {
    "get_workspace_profile": get_workspace_profile,
    "find_leads": find_leads,
    "find_affected_leads": find_affected_leads,
    "get_lead_snapshot": get_lead_snapshot,
    "get_conversation": get_conversation,
    "trace_message": trace_message,
    "get_ai_diagnostics": get_ai_diagnostics,
    "get_integration_health": get_integration_health,
    "get_workflow_trace": get_workflow_trace,
    "get_recent_errors": get_recent_errors,
    "get_runtime_health": get_runtime_health,
}


def execute_tool(*, name, organization, arguments):
    handler = TOOL_HANDLERS.get(str(name or ""))
    if handler is None:
        raise DiagnosticToolError("Unknown diagnostic tool.")
    return handler(organization=organization, arguments=arguments or {})
