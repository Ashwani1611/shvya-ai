import json
import logging
from decimal import Decimal, InvalidOperation

from celery import shared_task
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ai_engagement.services.ai_provider import (
    AIProviderError,
    AIProviderTransientError,
    OpenAIProvider,
)
from apps.calls.models import CallIntelligence, CallRecord


logger = logging.getLogger(__name__)


CALL_INTELLIGENCE_INSTRUCTIONS = """
You analyze a sales or service phone call for internal CRM use.
Use only the supplied transcript/notes. Never invent facts.
Return concise structured intelligence. If evidence is missing, use unknown/empty values.
Do not recommend or perform CRM stage changes. Do not infer sensitive personal traits.
AI score is 0-10 for commercial intent/next-step clarity, not employee performance.
""".strip()


CALL_INTELLIGENCE_SCHEMA = {
    "name": "shvya_call_intelligence",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "intent": {
                "type": "string",
                "enum": ["unknown", "low", "medium", "high"],
            },
            "sentiment": {"type": "string"},
            "outcome": {"type": "string"},
            "objections": {
                "type": "array",
                "items": {"type": "string"},
            },
            "buying_signals": {
                "type": "array",
                "items": {"type": "string"},
            },
            "competitors": {
                "type": "array",
                "items": {"type": "string"},
            },
            "next_action": {"type": "string"},
            "follow_up_at": {
                "type": ["string", "null"],
            },
            "ai_score": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 10,
            },
            "extracted_attributes": {
                "type": "object",
                "additionalProperties": {
                    "type": ["string", "number", "boolean", "null"],
                },
            },
        },
        "required": [
            "summary",
            "intent",
            "sentiment",
            "outcome",
            "objections",
            "buying_signals",
            "competitors",
            "next_action",
            "follow_up_at",
            "ai_score",
            "extracted_attributes",
        ],
        "additionalProperties": False,
    },
}


def _analysis_input(call, intelligence):
    transcript = str(intelligence.transcript or "").strip()
    notes = str(call.notes or "").strip()
    parts = [
        f"Direction: {call.direction}",
        f"Call status: {call.status}",
        f"Duration seconds: {call.duration_seconds}",
    ]
    if transcript:
        parts.append(f"Transcript:\n{transcript}")
    if notes:
        parts.append(f"Employee notes:\n{notes}")
    return "\n\n".join(parts)


def _score(value):
    if value is None:
        return None
    try:
        return max(Decimal("0"), min(Decimal("10"), Decimal(str(value))))
    except (InvalidOperation, ValueError, TypeError):
        return None


@shared_task(
    bind=True,
    name="calls.analyze_call_intelligence",
    max_retries=3,
    default_retry_delay=20,
)
def analyze_call_intelligence(self, call_id):
    try:
        call = (
            CallRecord.objects
            .select_related("organization", "lead")
            .get(pk=call_id)
        )
    except CallRecord.DoesNotExist:
        return {"status": "missing"}

    intelligence, _ = CallIntelligence.objects.get_or_create(call=call)
    input_text = _analysis_input(call, intelligence)
    if not (str(call.notes or "").strip() or str(intelligence.transcript or "").strip()):
        intelligence.analysis_status = CallIntelligence.AnalysisStatus.NOT_AVAILABLE
        intelligence.analysis_error = ""
        intelligence.save(update_fields=["analysis_status", "analysis_error", "updated_at"])
        return {"status": "no_evidence"}

    intelligence.analysis_status = CallIntelligence.AnalysisStatus.PROCESSING
    intelligence.analysis_error = ""
    intelligence.save(update_fields=["analysis_status", "analysis_error", "updated_at"])

    try:
        result = OpenAIProvider().generate_text(
            instructions=CALL_INTELLIGENCE_INSTRUCTIONS,
            input_text=input_text,
            response_schema=CALL_INTELLIGENCE_SCHEMA,
            metadata={
                "task": "call_intelligence",
                "organization_id": str(call.organization_id),
                "lead_id": str(call.lead_id or ""),
                "call_id": str(call.id),
            },
        )
        payload = json.loads(result.text)
    except AIProviderTransientError as exc:
        intelligence.analysis_status = CallIntelligence.AnalysisStatus.PENDING
        intelligence.analysis_error = str(exc)[:2000]
        intelligence.save(update_fields=["analysis_status", "analysis_error", "updated_at"])
        raise self.retry(exc=exc)
    except (AIProviderError, ValueError, TypeError, json.JSONDecodeError) as exc:
        intelligence.analysis_status = CallIntelligence.AnalysisStatus.FAILED
        intelligence.analysis_error = str(exc)[:2000]
        intelligence.save(update_fields=["analysis_status", "analysis_error", "updated_at"])
        logger.warning("Call Intelligence analysis failed for %s: %s", call.id, exc)
        return {"status": "failed", "error": str(exc)}

    follow_up_at = None
    raw_follow_up = payload.get("follow_up_at")
    if raw_follow_up:
        follow_up_at = parse_datetime(str(raw_follow_up))
        if follow_up_at and timezone.is_naive(follow_up_at):
            follow_up_at = timezone.make_aware(follow_up_at)

    intelligence.summary = str(payload.get("summary") or "")[:10000]
    intelligence.intent = payload.get("intent") or CallIntelligence.Intent.UNKNOWN
    intelligence.sentiment = str(payload.get("sentiment") or "")[:40]
    intelligence.outcome = str(payload.get("outcome") or "")[:100]
    intelligence.objections = list(payload.get("objections") or [])[:20]
    intelligence.buying_signals = list(payload.get("buying_signals") or [])[:20]
    intelligence.competitors = list(payload.get("competitors") or [])[:20]
    intelligence.next_action = str(payload.get("next_action") or "")[:255]
    intelligence.follow_up_at = follow_up_at
    intelligence.ai_score = _score(payload.get("ai_score"))
    intelligence.extracted_attributes = dict(payload.get("extracted_attributes") or {})
    intelligence.analysis_payload = {
        "provider_model": result.model,
        "source": "transcript" if intelligence.transcript else "employee_notes",
    }
    intelligence.analysis_status = CallIntelligence.AnalysisStatus.COMPLETED
    intelligence.analysis_error = ""
    intelligence.analyzed_at = timezone.now()
    intelligence.save()

    return {
        "status": "completed",
        "call_id": str(call.id),
        "intent": intelligence.intent,
        "ai_score": str(intelligence.ai_score) if intelligence.ai_score is not None else None,
    }
