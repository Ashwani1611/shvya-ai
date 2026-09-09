from __future__ import annotations

from typing import Any, TypedDict


class EngagementGraphState(TypedDict, total=False):
    """Compact orchestration state for one inbound lead turn.

    Database models and the full conversation are intentionally not copied into
    a durable LangGraph checkpoint. The existing Django context builder remains
    the source of truth and the graph only carries the current execution data.
    """

    service: Any
    legacy_engage: Any
    organization: Any
    lead: Any
    requested_knowledge_query: str
    supplied_context: Any
    caller_supplied_context: bool

    context: Any
    profile: dict[str, Any]
    runtime_policy: dict[str, Any]
    qualification_state: dict[str, Any]
    requirements: list[dict[str, Any]]
    latest_text: str
    latest_message_id: str

    route: str
    retrieval_query: str
    rag_retrieved: bool
    rag_chunks_before_filter: int
    rag_chunks_after_filter: int

    direct_decision: Any
    decision: Any
    validation_errors: list[str]

    grounding_approved: bool

