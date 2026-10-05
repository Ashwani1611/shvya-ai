from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

from apps.ai_engagement.services.trace_sanitizer import (
    preview,
    safe_error_message,
    sanitize,
)

logger = logging.getLogger(__name__)


@dataclass
class TraceBuffer:
    trace_id: str | None
    organization_id: str
    started_perf: float
    data: dict[str, Any] = field(default_factory=dict)


_CURRENT: ContextVar[TraceBuffer | None] = ContextVar("shvya_ai_trace", default=None)


def _merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        result = deepcopy(base)
        for key, value in patch.items():
            result[key] = _merge(result.get(key), value)
        return result
    if isinstance(base, list) and isinstance(patch, list):
        return (base + patch)[-100:]
    return deepcopy(patch)


def begin_trace(*, organization, lead=None, source_message=None, account=None) -> Token:
    """Create one fail-soft STARTED row and buffer later observations in memory."""
    trace_id = None
    try:
        from django.db import transaction

        from apps.ai_engagement.models import AITrace

        organization_id = getattr(organization, "id", None)
        safe_lead = (
            lead if getattr(lead, "organization_id", None) == organization_id else None
        )
        safe_account = (
            account
            if getattr(account, "organization_id", None) == organization_id
            else None
        )
        safe_source = (
            source_message
            if getattr(source_message, "organization_id", None) == organization_id
            else None
        )
        if safe_lead is not None and safe_source is not None:
            incoming = preview(getattr(safe_source, "body", ""), limit=500)
            is_instagram = getattr(safe_source, "conversation_id", None) is not None
            with transaction.atomic():
                trace = AITrace.objects.create(
                    organization=organization,
                    lead=safe_lead,
                    pipeline_id=getattr(safe_lead, "pipeline_id", None),
                    stage_id=getattr(safe_lead, "stage_id", None),
                    whatsapp_account_id=None if is_instagram else getattr(safe_account, "id", None),
                    source_inbound_message_id=getattr(safe_source, "id", None),
                    connection_type="instagram" if is_instagram else str(
                        getattr(safe_account, "connection_type", "") or "api"
                    ),
                    incoming_message_preview=incoming,
                    details={
                        "intent": {"status": "NOT_AVAILABLE"},
                        "input": {
                            "source_inbound_message_id": str(safe_source.id),
                            "message": preview(
                                getattr(safe_source, "body", ""), limit=1200
                            ),
                        },
                    },
                    status=AITrace.Status.STARTED,
                )
            trace_id = str(trace.id)
    except Exception:
        logger.exception("AI Trace creation failed; continuing AI turn without trace")

    buffer = TraceBuffer(
        trace_id=trace_id,
        organization_id=str(getattr(organization, "id", "") or ""),
        started_perf=time.perf_counter(),
        data={
            "status": "processing",
            "intent": {"status": "NOT_AVAILABLE"},
            "input": {
                "source_inbound_message_id": str(
                    getattr(source_message, "id", "") or ""
                ),
                "message": preview(
                    getattr(source_message, "body", ""), limit=1200
                ),
            },
        },
    )
    return _CURRENT.set(buffer)


def begin_sandbox_trace(*, organization, message) -> Token:
    """Persist one preview turn without inventing a lead or a channel message."""
    token = begin_trace(organization=organization)
    buffer = current()
    message = message if isinstance(message, str) else ""
    source_id = uuid.uuid4()
    record("input", {"source_inbound_message_id": str(source_id),
                     "source_kind": "sandbox_preview", "message": preview(message)})
    record("identity", {"connection_type": "sandbox", "execution_mode": "sandbox_preview"})
    try:
        from django.db import transaction
        from apps.ai_engagement.models import AITrace
        with transaction.atomic():
            trace = AITrace.objects.create(
                organization=organization, lead=None, connection_type="sandbox",
                source_inbound_message_id=source_id,
                incoming_message_preview=preview(message, limit=500),
                details=sanitize(buffer.data), status=AITrace.Status.STARTED,
            )
        buffer.trace_id = str(trace.pk)
    except Exception:
        logger.exception("Sandbox trace creation failed; preview remains unchanged")
    return token


def current() -> TraceBuffer | None:
    return _CURRENT.get()


def record(section: str, payload: dict[str, Any] | None = None, **fields) -> None:
    buffer = current()
    if buffer is None:
        return
    value = dict(payload or {})
    value.update(fields)
    value = sanitize(value)
    buffer.data[section] = _merge(buffer.data.get(section, {}), value)


def append(section: str, key: str, item: Any) -> None:
    buffer = current()
    if buffer is None:
        return
    section_data = buffer.data.setdefault(section, {})
    values = section_data.setdefault(key, [])
    if not isinstance(values, list):
        values = []
        section_data[key] = values
    values.append(sanitize(item))
    del values[:-100]


def mark_decision(*, decision) -> None:
    model = str(getattr(decision, "model", "") or "")
    path = "deterministic" if model.casefold() == "deterministic" else "model"
    reason_code = (
        getattr(decision, "reason_code", "") or getattr(decision, "reason", "")
    )
    record(
        "generation",
        {
            "execution_path": path.upper(),
            "model": model,
            "reason_code": reason_code,
            "should_engage": bool(getattr(decision, "should_engage", False)),
            "silence_rule": getattr(decision, "silence_rule", None),
            "generated_response": preview(
                getattr(decision, "message", ""), limit=2000
            ),
            "next_requirement_id": getattr(decision, "next_requirement_id", None),
            "file_document_id": getattr(decision, "file_document_id", None),
            "backend_revision": getattr(decision, "backend_revision", ""),
            "flow_version": getattr(decision, "flow_version", ""),
        },
    )
    record(
        "qualification",
        {"updates_proposed": getattr(decision, "qualification_updates", []) or []},
    )
    record(
        "crm_actions",
        {"proposed": getattr(decision, "crm_actions", []) or []},
    )


def mark_error(
    *,
    step: str,
    exc: BaseException,
    retryable: bool = False,
    code: str = "",
) -> None:
    record(
        "error",
        {
            "processing_step": step,
            "error_type": exc.__class__.__name__,
            "normalized_error_code": code,
            "safe_error_message": safe_error_message(exc),
            "retryable": retryable,
        },
    )


def finalize_from_result(result: Any) -> None:
    buffer = current()
    if buffer is None:
        return
    result = result if isinstance(result, dict) else {}
    raw_status = str(result.get("status") or "").casefold()
    reason = str(result.get("reason") or "")
    generation = buffer.data.get("generation") or {}
    should_engage = generation.get("should_engage")

    if raw_status == "completed":
        status = "silenced" if should_engage is False else "completed"
    elif "duplicate" in reason or reason in {
        "message_already_processed",
        "state_already_resolved",
    }:
        status = "duplicate"
    elif "conversation_changed" in reason or "stale" in reason:
        status = "stale"
    elif raw_status == "failed":
        status = "failed"
    else:
        status = "blocked"

    accepted = status in {"completed", "silenced"}
    record(
        "finalization",
        {
            "permission_recheck": "observed_by_existing_runtime",
            "account_recheck": "observed_by_existing_runtime",
            "freshness_check": "rejected" if status == "stale" else "accepted",
            "duplicate_response_check": (
                "rejected" if status == "duplicate" else "accepted"
            ),
            "decision_accepted": accepted,
            "reason": reason,
            "outbound_row_created": bool(result.get("message_id")),
            "outbound_queued": bool(result.get("message_id")),
            "source_message_id": result.get("source_message_id"),
            "whatsapp_24h_eligibility": (
                "rejected"
                if reason == "whatsapp_24h_window_expired"
                else "accepted"
                if result.get("message_id")
                else "not_reached_or_not_applicable"
            ),
        },
    )
    if (buffer.data.get("identity") or {}).get("connection_type") == "instagram":
        record("finalization", {"whatsapp_24h_eligibility": "not_applicable",
                                "delivery_owner": "instagram_send_task"})
    proposed_updates = list(
        (buffer.data.get("qualification") or {}).get("updates_proposed") or []
    )
    if proposed_updates:
        record(
            "qualification",
            {
                "updates_accepted": proposed_updates if accepted else [],
                "updates_rejected": [] if accepted else proposed_updates,
            },
        )
    buffer.data["status"] = status
    buffer.data["reason_code"] = reason or generation.get("reason_code", "")
    if result.get("message_id"):
        buffer.data["outbound_message_id"] = str(result["message_id"])


def flush(*, reset_token: Token | None = None) -> None:
    buffer = current()
    try:
        if buffer is None or not buffer.trace_id:
            return
        from apps.ai_engagement.models import AITrace

        elapsed_ms = max(
            int((time.perf_counter() - buffer.started_perf) * 1000),
            0,
        )
        from apps.ai_engagement.services.turn_health import evaluate_turn_health
        from apps.core.observability import increment

        quality = evaluate_turn_health(buffer.data, total_ms=elapsed_ms)
        buffer.data["quality"] = quality
        increment(
            "ai.turn_health",
            labels={"band": quality["band"]},
        )
        generation = buffer.data.get("generation") or {}
        details = sanitize(
            {
                key: value
                for key, value in buffer.data.items()
                if key not in {"status", "reason_code", "outbound_message_id"}
            }
        )
        updates = {
            "status": buffer.data.get("status", "processing"),
            "reason_code": str(
                buffer.data.get("reason_code")
                or generation.get("reason_code")
                or ""
            )[:96],
            "execution_path": str(generation.get("execution_path") or "")[:32],
            "model_name": str(generation.get("model") or "")[:150],
            "response_preview": preview(
                generation.get("generated_response", ""), limit=500
            ),
            "details": details,
            "total_ms": elapsed_ms,
            "completed_at": timezone.now(),
        }
        outbound_id = buffer.data.get("outbound_message_id")
        if outbound_id:
            updates["outbound_message_id"] = outbound_id
        from django.db import transaction
        with transaction.atomic():
            rows = AITrace.objects.filter(pk=buffer.trace_id, organization_id=buffer.organization_id)
            persisted = rows.select_for_update().first()
            if persisted is not None:
                # A send task or on-commit callback may finish before the turn
                # flush. Never erase its newer delivery observation with this
                # generation buffer, which does not own provider status.
                stored = persisted.details if isinstance(persisted.details, dict) else {}
                if isinstance(stored.get("delivery"), dict):
                    updates["details"] = sanitize({"delivery": stored["delivery"], **{
                        key: value for key, value in updates["details"].items() if key != "delivery"
                    }})
                    if persisted.outbound_message_id:
                        updates["outbound_message_id"] = persisted.outbound_message_id
                rows.update(**updates)
    except Exception:
        logger.exception("AI Trace flush failed; normal AI result remains authoritative")
    finally:
        if reset_token is not None:
            try:
                _CURRENT.reset(reset_token)
            except Exception:
                logger.exception("AI Trace context reset failed")


def safe_delivery_update(
    *,
    organization_id,
    source_message_id,
    outbound_message,
) -> None:
    """Persist delivery observation after the business transaction commits."""
    try:
        from apps.ai_engagement.models import AITrace
        from apps.channels.models import WhatsAppMessage

        from django.db import transaction
        with transaction.atomic():
            trace = AITrace.objects.select_for_update().filter(
                organization_id=organization_id,
                source_inbound_message_id=source_message_id,
                connection_type="instagram" if getattr(outbound_message, "conversation_id", None) is not None
                else str(getattr(getattr(outbound_message, "account", None), "connection_type", "") or "api"),
            ).order_by("-started_at", "-id").first()
            if trace is None:
                return
            status = str(getattr(outbound_message, "status", "") or "")
            details = deepcopy(trace.details or {})
            delivery = details.setdefault("delivery", {})
            delivery["status"] = status
            delivery["outbound_message_id"] = str(getattr(outbound_message, "id", "") or "")
            if status in {WhatsAppMessage.Status.DELIVERED, WhatsAppMessage.Status.READ}:
                delivery["delivered_at"] = timezone.now().isoformat()
            if status == WhatsAppMessage.Status.FAILED:
                delivery["error"] = safe_error_message(getattr(outbound_message, "error", ""))

            from apps.ai_engagement.services.turn_health import evaluate_turn_health

            details["quality"] = evaluate_turn_health(
                {
                    **details,
                    "status": trace.status,
                    "delivery": delivery,
                },
                total_ms=trace.total_ms,
            )
            AITrace.objects.filter(pk=trace.pk, organization_id=organization_id).update(
                outbound_message_id=getattr(outbound_message, "id", None), details=sanitize(details),
            )
    except Exception:
        logger.exception(
            "AI Trace delivery update failed; delivery state remains unchanged"
        )
