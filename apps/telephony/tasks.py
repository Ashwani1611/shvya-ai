import json
import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ai_engagement.services.ai_provider import (
    AIProviderConfigurationError, AIProviderError, OpenAIProvider,
)
from apps.crm.models import Lead, LeadCall

from .models import CallEvent, CallIntelligenceResult, CallRecord
from .services import (
    call_analysis_hash, capture_manual_crm_call, publish_call_analysis,
    reconcile_call, request_call_analysis,
)

logger = logging.getLogger(__name__)

CALL_INTELLIGENCE_SCHEMA = {
    "name": "shvya_call_intelligence",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "intent": {"type": "string", "enum": ["unknown", "low", "medium", "high"]},
            "sentiment": {"type": "string", "enum": ["unknown", "negative", "neutral", "positive"]},
            "outcome": {"type": "string"},
            "objections": {"type": "array", "items": {"type": "string"}},
            "buying_signals": {"type": "array", "items": {"type": "string"}},
            "competitor": {"type": "string"},
            "budget": {"type": "string"},
            "timeline": {"type": "string"},
            "product_interest": {"type": "string"},
            "decision_maker": {"type": "string", "enum": ["unknown", "yes", "no"]},
            "next_action": {"type": "string"},
            "follow_up_at": {"type": "string"},
            "ai_score": {"type": "integer", "minimum": 0, "maximum": 10},
            "qualification_score": {"type": "integer", "minimum": 0, "maximum": 10},
            "agent_metrics": {
                "type": "object",
                "properties": {
                    "questions_asked": {"type": "integer", "minimum": 0},
                    "discovery_quality": {"type": "integer", "minimum": 0, "maximum": 10},
                    "objection_handling": {"type": "integer", "minimum": 0, "maximum": 10},
                    "next_step_clarity": {"type": "integer", "minimum": 0, "maximum": 10},
                    "talk_ratio_agent": {"type": "number", "minimum": 0, "maximum": 100},
                    "talk_ratio_lead": {"type": "number", "minimum": 0, "maximum": 100},
                    "silence_percent": {"type": "number", "minimum": 0, "maximum": 100},
                    "interruptions": {"type": "integer", "minimum": 0},
                },
                "required": [
                    "questions_asked", "discovery_quality", "objection_handling",
                    "next_step_clarity", "talk_ratio_agent", "talk_ratio_lead",
                    "silence_percent", "interruptions",
                ],
                "additionalProperties": False,
            },
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
            "attributes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["key", "value", "evidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "summary", "intent", "sentiment", "outcome", "objections",
            "buying_signals", "competitor", "budget", "timeline",
            "product_interest", "decision_maker", "next_action", "follow_up_at",
            "ai_score", "qualification_score", "agent_metrics",
            "compliance_flags", "attributes",
        ],
        "additionalProperties": False,
    },
}

CALL_INTELLIGENCE_INSTRUCTIONS = """
Analyze a completed SHVYA sales call using only supplied call metadata, manual
notes, transcript/speaker data and CRM context. Do not invent facts. Empty or
unsupported information stays empty/unknown. Intent means commercial buying
intent. Scores must be grounded in the supplied conversation. Agent metrics
must be conservative when speaker/timing evidence is incomplete. Attribute
candidates require explicit evidence. This analysis is advisory: never claim
that a CRM stage, attribute, reminder or outbound message was changed. Those
mutations are owned by validated SHVYA Workflows/backend actions.
Keep the summary under 120 words and each evidence list to at most five items.
""".strip()


def _event(call, event_type, payload=None):
    CallEvent.objects.create(
        event_uuid=uuid.uuid4(),
        organization=call.organization,
        call=call,
        device=call.device,
        user=call.user,
        event_type=event_type,
        occurred_at=timezone.now(),
        payload=payload or {},
    )


def _score(value):
    try:
        return min(10, max(0, int(value)))
    except (TypeError, ValueError):
        return 0


def _analysis_failed(call, input_hash, message, *, retry=False):
    CallRecord.objects.filter(
        pk=call.id, analysis_input_hash=input_hash,
        analysis_status=CallRecord.AnalysisStatus.PROCESSING,
    ).update(
        analysis_status=(CallRecord.AnalysisStatus.QUEUED if retry
                         else CallRecord.AnalysisStatus.FAILED),
        analysis_error=message,
        analysis_updated_at=timezone.now(),
    )


@shared_task(bind=True, max_retries=2, acks_late=True)
def analyze_call_intelligence(self, call_id, input_hash=None):
    with transaction.atomic():
        call = (
            CallRecord.objects
            .select_for_update(of=("self",))
            .select_related(
                "organization", "device", "lead", "lead__pipeline", "lead__stage", "user"
            )
            .filter(pk=call_id).first()
        )
        if call is None or (not call.notes.strip() and not call.transcript.strip()):
            return
        input_hash = input_hash or call.analysis_input_hash or call_analysis_hash(call)
        if input_hash != call_analysis_hash(call) or call.analysis_status in {
            CallRecord.AnalysisStatus.COMPLETED, CallRecord.AnalysisStatus.PROCESSING,
        }:
            return
        if call.analysis_attempts >= 3:
            call.analysis_status = CallRecord.AnalysisStatus.FAILED
            call.analysis_error = "Analysis could not finish. Save & analyze to retry."
            call.save(update_fields=["analysis_status", "analysis_error"])
            return
        call.analysis_input_hash = input_hash
        call.analysis_status = CallRecord.AnalysisStatus.PROCESSING
        call.analysis_error = ""
        call.analysis_attempts += 1
        call.analysis_updated_at = timezone.now()
        call.save(update_fields=[
            "analysis_input_hash", "analysis_status", "analysis_error",
            "analysis_attempts", "analysis_updated_at",
        ])

    _event(call, CallEvent.Type.AI_ANALYSIS_STARTED)
    payload = {
        "call": {
            "direction": call.direction,
            "status": call.status,
            "talk_duration_seconds": call.talk_duration_seconds,
            "ring_duration_seconds": call.ring_duration_seconds,
            "notes": call.notes,
            "disposition": call.disposition,
            "transcript": call.transcript,
            "speaker_segments": call.transcript_speakers,
            "source": call.source,
            "provider": call.provider,
        },
        "lead": (
            {
                "name": call.lead.name,
                "pipeline": call.lead.pipeline.name,
                "stage": call.lead.stage.name,
                "attributes": call.lead.attributes,
            }
            if call.lead_id
            else None
        ),
    }

    try:
        result = OpenAIProvider().generate_text(
            instructions=CALL_INTELLIGENCE_INSTRUCTIONS,
            input_text=json.dumps(payload, ensure_ascii=False),
            metadata={
                "organization_id": str(call.organization_id),
                "lead_id": str(call.lead_id or ""),
                "call_id": str(call.id),
                "task": "call_intelligence",
            },
            response_schema=CALL_INTELLIGENCE_SCHEMA,
        )
        data = json.loads(result.text)
        if not isinstance(data, dict) or not str(data.get("summary") or "").strip():
            raise ValueError("Missing call summary.")
    except AIProviderError as exc:
        logger.warning("Call Intelligence provider failed for %s (%s)", call_id, type(exc).__name__)
        retry = bool(getattr(exc, "retryable", False) and call.analysis_attempts < 3)
        message = ("AI is not configured. Ask your administrator to check the OpenAI configuration."
                   if isinstance(exc, AIProviderConfigurationError)
                   else "Analysis could not finish. Check AI availability and credits, then Save & analyze to retry.")
        _analysis_failed(call, input_hash, message, retry=retry)
        if retry:
            raise self.retry(exc=exc, countdown=15)
        return
    except (ValueError, TypeError) as exc:
        logger.warning("Call Intelligence output failed for %s", call_id)
        retry = call.analysis_attempts < 3
        _analysis_failed(
            call, input_hash, "AI returned an incomplete result. Save & analyze to retry.", retry=retry,
        )
        if retry:
            raise self.retry(exc=exc, countdown=15)
        return
    except Exception:
        logger.exception("Call Intelligence failed for %s", call_id)
        _analysis_failed(call, input_hash, "Analysis could not finish. Save & analyze to retry.")
        return

    try:
        follow_up_at = parse_datetime(str(data.get("follow_up_at") or "").strip()) or None
    except ValueError:
        follow_up_at = None
    if follow_up_at and timezone.is_naive(follow_up_at):
        follow_up_at = timezone.make_aware(follow_up_at, timezone.get_current_timezone())

    defaults = {
            "summary": str(data.get("summary") or "")[:20000],
            "intent": data.get("intent") or "unknown",
            "sentiment": data.get("sentiment") or "unknown",
            "outcome": str(data.get("outcome") or "")[:255],
            "objections": data.get("objections") or [],
            "buying_signals": data.get("buying_signals") or [],
            "competitor": str(data.get("competitor") or "")[:255],
            "budget": str(data.get("budget") or "")[:255],
            "timeline": str(data.get("timeline") or "")[:255],
            "product_interest": str(data.get("product_interest") or "")[:255],
            "decision_maker": str(data.get("decision_maker") or "unknown")[:16],
            "follow_up_at": follow_up_at,
            "next_action": str(data.get("next_action") or "")[:255],
            "ai_score": _score(data.get("ai_score")),
            "qualification_score": _score(data.get("qualification_score")),
            "agent_metrics": data.get("agent_metrics") or {},
            "compliance_flags": data.get("compliance_flags") or [],
            "extracted_attributes": data.get("attributes") or [],
            "raw_analysis": data,
            "model": str(result.model or "")[:100],
            "analyzed_at": timezone.now(),
    }
    with transaction.atomic():
        current = CallRecord.objects.select_for_update().filter(
            pk=call.id, analysis_input_hash=input_hash,
            analysis_status=CallRecord.AnalysisStatus.PROCESSING,
        ).first()
        if current is None:
            return  # Notes changed while the provider was working.
        intelligence, _ = CallIntelligenceResult.objects.update_or_create(
            call=current, defaults=defaults,
        )
        current.analysis_status = CallRecord.AnalysisStatus.COMPLETED
        current.analysis_error = ""
        current.analysis_updated_at = timezone.now()
        current.save(update_fields=["analysis_status", "analysis_error", "analysis_updated_at"])
        _event(
            current, CallEvent.Type.AI_ANALYSIS_COMPLETED,
            {
                "intelligence_id": str(intelligence.id), "intent": intelligence.intent,
                "ai_score": intelligence.ai_score,
                "qualification_score": intelligence.qualification_score,
            },
        )


@shared_task
def recover_call_intelligence():
    """Bounded legacy repair and recovery after broker/worker interruptions."""
    limit = 100
    matches = Lead.objects.filter(
        organization_id=OuterRef("organization_id"), phone=OuterRef("phone_number"),
    )
    unlinked = CallRecord.objects.filter(
        Q(lead__isnull=True) | Q(crm_call__isnull=True, ended_at__isnull=False),
    ).annotate(has_lead=Exists(matches)).filter(has_lead=True)
    for call_id in list(unlinked.values_list("id", flat=True)[:limit]):
        reconcile_call(call_id)
    historical = LeadCall.objects.filter(
        intelligence_record__isnull=True, status__in=["completed", "no_response", "busy"],
    ).select_related("lead", "lead__organization", "user").order_by("-called_at")[:limit]
    for crm_call in historical:
        capture_manual_crm_call(crm_call)
    pending = CallRecord.objects.filter(
        analysis_status=CallRecord.AnalysisStatus.NOT_REQUESTED, intelligence__isnull=True,
    ).exclude(notes="", transcript="")
    for call_id in list(pending.values_list("id", flat=True)[:limit]):
        request_call_analysis(call_id)
    stale = CallRecord.objects.filter(
        analysis_status__in=[CallRecord.AnalysisStatus.QUEUED, CallRecord.AnalysisStatus.PROCESSING],
        analysis_updated_at__lt=timezone.now() - timedelta(minutes=5),
    ).order_by("analysis_updated_at")
    for call_id in list(stale.values_list("id", flat=True)[:limit]):
        with transaction.atomic():
            call = CallRecord.objects.select_for_update().filter(pk=call_id).first()
            if call is None or call.analysis_status not in {"queued", "processing"}:
                continue
            if call.analysis_updated_at and call.analysis_updated_at >= timezone.now() - timedelta(minutes=5):
                continue
            if call.analysis_attempts >= 3:
                call.analysis_status = CallRecord.AnalysisStatus.FAILED
                call.analysis_error = "Analysis could not finish. Save & analyze to retry."
            else:
                call.analysis_status = CallRecord.AnalysisStatus.QUEUED
                transaction.on_commit(
                    lambda call_id=call.id, digest=call.analysis_input_hash:
                    publish_call_analysis(call_id, digest)
                )
            call.analysis_updated_at = timezone.now()
            call.save(update_fields=["analysis_status", "analysis_error", "analysis_updated_at"])
