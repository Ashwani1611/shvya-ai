"""Celery task compatibility surface for AI engagement.

Task implementations are split by responsibility, while explicit Celery task
names and imports from apps.ai_engagement.tasks remain stable.
"""

from .bumpups import dispatch_bump_ups
from .credits import reconcile_credit_settlements
from .engagement import (
    _execute_ai_engagement_response,
    _execute_ai_engagement_response_impl,
    _has_existing_ai_response,
    _latest_whatsapp_message,
    _persist_engagement_answers,
    _whatsapp_send_eligible,
    generate_ai_engagement_response,
    recover_api_engagement,
)
from .enrichment import flush_background_enrichment
from .knowledge import (
    ingest_and_index_document,
    ingest_and_index_url_source,
    reindex_document_embeddings,
)
from .qualification import generate_lead_qualification
from .summaries import generate_internal_conversation_summary


__all__ = [
    "flush_background_enrichment",
    "reconcile_credit_settlements",
    "dispatch_bump_ups",
    "generate_internal_conversation_summary",
    "generate_lead_qualification",
    "generate_ai_engagement_response",
    "recover_api_engagement",
    "ingest_and_index_document",
    "ingest_and_index_url_source",
    "reindex_document_embeddings",
]
