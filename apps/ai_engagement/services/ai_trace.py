from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_TEXT = 2000
MAX_PREVIEW = 500
MAX_LIST = 50
MAX_DEPTH = 6
MAX_JSON_BYTES = 32768

_SECRET_KEY_RE = re.compile(
    r"(?:access[_-]?token|api[_-]?key|authorization|password|passwd|cookie|"
    r"webhook[_-]?secret|session[_-]?secret|database[_-]?url|private[_-]?key|"
    r"smtp[_-]?(?:password|credential)|client[_-]?secret)",
    re.IGNORECASE,
)


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "")[:limit]


def sanitize_trace_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_DEPTH:
        return "[bounded]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        cleaned = {}
        for raw_key, raw_value in list(value.items())[:MAX_LIST]:
            key = _text(raw_key, 120)
            cleaned[key] = "[redacted]" if _SECRET_KEY_RE.search(key) else sanitize_trace_value(raw_value, depth=depth + 1)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [sanitize_trace_value(item, depth=depth + 1) for item in list(value)[:MAX_LIST]]
    return _text(value)


def _bounded_details(value: dict[str, Any]) -> dict[str, Any]:
    cleaned = sanitize_trace_value(value)
    if not isinstance(cleaned, dict):
        return {}
    encoded = json.dumps(cleaned, default=str, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_JSON_BYTES:
        return cleaned
    priority = ("permission", "intent", "qualification", "rag", "generation", "crm_actions", "finalization", "performance", "provider_usage", "errors")
    compact = {key: cleaned[key] for key in priority if key in cleaned}
    if len(json.dumps(compact, default=str, separators=(",", ":")).encode("utf-8")) <= MAX_JSON_BYTES:
        return compact
    return {"trace_payload_truncated": True}


def _permission_snapshot(*, organization, lead, latest_inbound=None) -> dict[str, Any]:
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.ai_permissions import AIPermissionService

    org_info = OrgInfo.objects.filter(organization=organization).first()
    account = getattr(latest_inbound, "account", None)
    result = {
        "organization_ai_enabled": bool(getattr(org_info, "ai_enabled", False)),
        "pipeline_ai_enabled": bool(getattr(getattr(lead, "pipeline", None), "ai_enabled", True)),
        "stage_ai_enabled": bool(getattr(getattr(lead, "stage", None), "ai_on", True)),
        "lead_ai_enabled": bool(getattr(lead, "ai_enabled", False)),
        "account_valid": bool(account and account.organization_id == organization.id and getattr(account, "is_active", False) and str(getattr(account, "status", "")) == "connected"),
        "pipeline_account_mapping_valid": None,
        "final_permission_result": None,
        "reason": "",
    }
    try:
        decision = AIPermissionService().evaluate(organization=organization, lead=lead, latest_inbound=latest_inbound)
        result["final_permission_result"] = bool(decision.allowed)
        result["reason"] = decision.reason
        result["pipeline_account_mapping_valid"] = decision.reason not in {
            "pipeline_whatsapp_number_missing", "pipeline_whatsapp_account_mismatch", "whatsapp_account_organization_mismatch",
            "whatsapp_account_inactive", "whatsapp_account_not_connected", "unsupported_whatsapp_connection_type", "whatsapp_account_number_missing",
        }
    except Exception:
        logger.exception("AI trace could not snapshot permission state")
    return result


def _qualification_snapshot(*, organization, lead) -> dict[str, Any]:
    try:
        from apps.ai_engagement.models import OrgInfo
        from apps.ai_engagement.services.organization_profile import compile_org_ai_profile
        from apps.ai_engagement.services.qualification_state import next_requirement, requirements_for_lead, state_for_lead

        org_info = OrgInfo.objects.filter(organization=organization).first()
        profile = compile_org_ai_profile(organization_name=organization.name, org_info=org_info)
        requirements = requirements_for_lead(lead, (profile.get("qualification") or {}).get("requirements") or [])
        state = state_for_lead(lead, requirements=requirements)
        states = state.get("requirement_states") or {}
        current = next_requirement(requirements, states)
        answered = [str(req.get("id")) for req in requirements if (states.get(str(req.get("id"))) or {}).get("status") == "answered"]
        return {
            "current_expected_requirement": sanitize_trace_value(current or {}),
            "already_answered_requirement_ids": answered,
            "qualification_status": state.get("qualification_status"),
            "next_requirement": sanitize_trace_value(current or {}),
            "flow_version": state.get("flow_version") or "",
        }
    except Exception:
        logger.exception("AI trace could not snapshot qualification state")
        return {}


class AITraceRecorder:
    @classmethod
    def start(cls, *, organization, lead, source_message, account=None, connection_type=None):
        try:
            from apps.ai_engagement.models import AITrace
            account = account or getattr(source_message, "account", None)
            connection = connection_type or str(getattr(account, "connection_type", "") or "")
            details = {
                "input": {"source_inbound_message_id": str(source_message.id), "message": _text(getattr(source_message, "body", ""), MAX_TEXT)},
                "permission": _permission_snapshot(organization=organization, lead=lead, latest_inbound=source_message),
                "intent": {"status": "NOT_AVAILABLE"},
                "qualification": _qualification_snapshot(organization=organization, lead=lead),
                "rag": {"invoked": False, "retrieval_path": "NONE", "chunks": []},
                "generation": {},
                "crm_actions": {"proposed": [], "accepted": [], "rejected": []},
                "finalization": {}, "performance": {}, "provider_usage": {}, "errors": [],
            }
            return AITrace.objects.create(
                organization=organization, lead=lead, pipeline_id=getattr(lead, "pipeline_id", None), stage_id=getattr(lead, "stage_id", None),
                whatsapp_account_id=getattr(account, "id", None), source_inbound_message_id=source_message.id,
                connection_type=(AITrace.ConnectionType.HOSTED if connection == "hosted" else AITrace.ConnectionType.API),
                status=AITrace.Status.STARTED, incoming_message_preview=_text(getattr(source_message, "body", ""), MAX_PREVIEW), details=_bounded_details(details),
            )
        except Exception:
            logger.exception("AI trace creation failed; engagement will continue")
            return None

    @classmethod
    def update(cls, trace, *, status=None, reason_code=None, execution_path=None, model_name=None, response=None, outbound_message_id=None, merge=None, completed=False, total_ms=None):
        if trace is None:
            return None
        try:
            from apps.ai_engagement.models import AITrace
            current = AITrace.objects.filter(pk=trace.pk).first()
            if current is None:
                return trace
            if status:
                current.status = status
            if reason_code is not None:
                current.reason_code = _text(reason_code, 96)
            if execution_path is not None:
                current.execution_path = _text(execution_path, 32)
            if model_name is not None:
                current.model_name = _text(model_name, 150)
            if response is not None:
                current.response_preview = _text(response, MAX_PREVIEW)
            if outbound_message_id:
                current.outbound_message_id = outbound_message_id
            if total_ms is not None:
                current.total_ms = max(0, int(total_ms))
            details = deepcopy(current.details or {})
            for key, value in (merge or {}).items():
                if isinstance(value, dict) and isinstance(details.get(key), dict):
                    details[key].update(value)
                else:
                    details[key] = value
            current.details = _bounded_details(details)
            if completed:
                current.completed_at = timezone.now()
            current.save()
            return current
        except Exception:
            logger.exception("AI trace update failed; engagement will continue")
            return trace

    @classmethod
    def finish_from_result(cls, trace, *, result, elapsed_ms):
        if trace is None:
            return None
        result = result if isinstance(result, dict) else {}
        raw_status = str(result.get("status") or "").casefold()
        reason = str(result.get("reason") or "")
        status = "completed"
        if raw_status == "failed": status = "failed"
        elif reason in {"duplicate_ai_response", "message_already_processed", "state_already_resolved"}: status = "duplicate"
        elif "conversation_changed" in reason: status = "stale"
        elif raw_status == "skipped": status = "blocked"
        elif reason == "no_engagement": status = "silenced"
        accepted = status in {"completed", "silenced"}
        details = deepcopy(getattr(trace, "details", {}) or {})
        proposed_q = ((details.get("qualification") or {}).get("qualification_updates_proposed") or [])
        proposed_actions = ((details.get("crm_actions") or {}).get("proposed") or [])
        merge = {
            "qualification": {
                "qualification_updates_accepted": proposed_q if accepted else [],
                "qualification_updates_rejected": [] if accepted else proposed_q,
                "final_state": _qualification_snapshot(organization=trace.organization, lead=trace.lead),
            },
            "crm_actions": {"accepted": proposed_actions if accepted else [], "rejected": [] if accepted else proposed_actions, "rejection_reason": "" if accepted else reason},
            "finalization": {
                "decision_accepted": accepted, "reason": reason, "outbound_row_created": bool(result.get("message_id")),
                "outbound_queued": bool(result.get("message_id")), "outbound_delivered": False, "delivery_observable": False,
            },
            "performance": {"total_ms": int(elapsed_ms)},
        }
        return cls.update(trace, status=status, reason_code=reason, outbound_message_id=result.get("message_id"), merge=merge, completed=True, total_ms=elapsed_ms)


class TraceTimer:
    def __init__(self):
        self.started = time.perf_counter()
    def ms(self):
        return int((time.perf_counter() - self.started) * 1000)
