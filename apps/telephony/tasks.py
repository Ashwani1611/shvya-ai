import json
import logging
import uuid

from celery import shared_task
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ai_engagement.services.ai_provider import AIProviderError, OpenAIProvider

from .models import CallEvent, CallIntelligenceResult, CallRecord

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


@shared_task(bind=True, max_retries=2)
def analyze_call_intelligence(self, call_id):
    try:
        call = (
            CallRecord.objects
            .select_related(
                "organization", "device", "lead", "lead__pipeline", "lead__stage", "user"
            )
            .get(pk=call_id)
        )
    except CallRecord.DoesNotExist:
        return

    if not (call.notes or "").strip() and not (call.transcript or "").strip():
        return

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
    except AIProviderError as exc:
        logger.warning("Call Intelligence provider failed for %s: %s", call_id, exc)
        if getattr(exc, "retryable", False):
            raise self.retry(exc=exc, countdown=15)
        return
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Call Intelligence output failed for %s: %s", call_id, exc)
        return

    follow_up_at = parse_datetime(str(data.get("follow_up_at") or "").strip()) or None
    if follow_up_at and timezone.is_naive(follow_up_at):
        follow_up_at = timezone.make_aware(follow_up_at, timezone.get_current_timezone())

    intelligence, _ = CallIntelligenceResult.objects.update_or_create(
        call=call,
        defaults={
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
        },
    )
    _event(
        call,
        CallEvent.Type.AI_ANALYSIS_COMPLETED,
        {
            "intelligence_id": str(intelligence.id),
            "intent": intelligence.intent,
            "ai_score": intelligence.ai_score,
            "qualification_score": intelligence.qualification_score,
        },
    )
