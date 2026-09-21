import json
import logging

from celery import shared_task
from django.utils import timezone

from apps.ai_engagement.services.ai_provider import AIProviderError, OpenAIProvider

from .models import CallIntelligenceResult, CallRecord

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
            "next_action": {"type": "string"},
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
            "next_action", "attributes",
        ],
        "additionalProperties": False,
    },
}

CALL_INTELLIGENCE_INSTRUCTIONS = """
Analyze a completed SHVYA sales call using only the supplied call metadata,
manual notes and CRM context. Do not invent facts. Empty information stays
empty. Intent means commercial buying intent, not emotion. Extract only facts
explicitly supported by the supplied notes. Return the strict schema. This
analysis is advisory and must never claim that a CRM stage, attribute, reminder
or outbound message was changed.
""".strip()


@shared_task(bind=True, max_retries=2)
def analyze_call_intelligence(self, call_id):
    try:
        call = (
            CallRecord.objects
            .select_related("organization", "lead", "lead__pipeline", "lead__stage", "user")
            .get(pk=call_id)
        )
    except CallRecord.DoesNotExist:
        return

    if not (call.notes or "").strip():
        return

    payload = {
        "call": {
            "direction": call.direction,
            "status": call.status,
            "talk_duration_seconds": call.talk_duration_seconds,
            "ring_duration_seconds": call.ring_duration_seconds,
            "notes": call.notes,
            "disposition": call.disposition,
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

    CallIntelligenceResult.objects.update_or_create(
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
            "next_action": str(data.get("next_action") or "")[:255],
            "extracted_attributes": data.get("attributes") or [],
            "raw_analysis": data,
            "model": str(result.model or "")[:100],
            "analyzed_at": timezone.now(),
        },
    )
