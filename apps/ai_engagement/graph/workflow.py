from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from typing import Literal

from langgraph.graph import END, START, StateGraph

from apps.ai_engagement.graph.runtime_policy import get_runtime_policy
from apps.ai_engagement.graph.state import EngagementGraphState
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
)
from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION,
    REQUIREMENT_ANSWERED,
    apply_unambiguous_reply,
    state_for_lead,
)


logger = logging.getLogger(__name__)

_OPT_OUT_PATTERNS = (
    r"^stop$",
    r"^unsubscribe$",
    r"^opt\s*out$",
    r"^do\s+not\s+(?:message|contact|text)(?:\s+me)?$",
    r"^don't\s+(?:message|contact|text)(?:\s+me)?$",
    r"^dont\s+(?:message|contact|text)(?:\s+me)?$",
)


def _min_rag_similarity() -> float:
    try:
        value = float(os.getenv("AI_RAG_MIN_SIMILARITY", "0.38"))
    except (TypeError, ValueError):
        value = 0.38
    return min(max(value, -1.0), 1.0)


def _prepare(state: EngagementGraphState) -> dict:
    service = state["service"]
    organization = state["organization"]
    lead = state["lead"]
    supplied_context = state.get("supplied_context")
    caller_supplied = supplied_context is not None

    if organization is None:
        from apps.ai_engagement.services.engagement import EngagementError

        raise EngagementError("Organization is required.")
    if lead is None:
        from apps.ai_engagement.services.engagement import EngagementError

        raise EngagementError("Lead is required.")

    context = supplied_context
    if context is None:
        context = service.context_builder.build(
            organization=organization,
            lead=lead,
            knowledge_query=None,
            message_limit=service.MESSAGE_LIMIT,
            knowledge_limit=service.KNOWLEDGE_LIMIT,
            note_limit=service.NOTE_LIMIT,
        )

    service._validate_context_scope(
        organization=organization,
        lead=lead,
        context=context,
    )
    profile = compile_org_ai_profile_from_context(context.organization or {})
    policy = get_runtime_policy(organization=organization, profile=profile)

    # Attach the compact policy to this immutable runtime context. Existing
    # context consumers remain unchanged because it is an additive key.
    org_context = dict(context.organization or {})
    org_context["_runtime_policy"] = policy
    context = replace(context, organization=org_context)

    requirements = profile.get("qualification", {}).get("requirements", [])
    qualification_state = state_for_lead(lead, requirements=requirements)

    return {
        "context": context,
        "profile": profile,
        "runtime_policy": policy,
        "requirements": requirements,
        "qualification_state": qualification_state,
        "latest_text": service._latest_inbound_text(context=context),
        "latest_message_id": service._latest_inbound_message_id(context=context),
        "caller_supplied_context": caller_supplied,
        "validation_errors": [],
    }


def _deterministic_extract(state: EngagementGraphState) -> dict:
    """Handle only high-confidence, zero-credit qualification answers.

    Free-form interpretation stays with the model. This node only binds an
    obvious short answer to the exact requirement the application recorded as
    last asked, preserving the evidence controls already present in SHVYA.
    """

    if state.get("caller_supplied_context"):
        return {}

    service = state["service"]
    lead = state["lead"]
    requirements = state.get("requirements") or []
    if not requirements:
        return {}

    direct = apply_unambiguous_reply(
        lead=lead,
        requirements=requirements,
        text=state.get("latest_text", ""),
        source_message_id=state.get("latest_message_id", ""),
    )
    qualification_state = direct["state"]
    updates: dict = {"qualification_state": qualification_state}

    direct_next = direct.get("next_requirement")
    profile = state.get("profile") or {}
    context = state["context"]
    if (
        not profile.get("communication", {}).get("custom_instructions")
        and not profile.get("communication", {}).get("languages")
        and not (context.pipeline or {}).get("attribute_definitions")
        and direct.get("changed")
        and direct.get("answer_status") == REQUIREMENT_ANSWERED
        and qualification_state.get("engagement_mode") == MODE_QUALIFICATION
        and isinstance(direct_next, dict)
        and direct_next.get("can_direct_ask")
        and str(direct_next.get("question") or "").strip()
    ):
        from apps.ai_engagement.services.engagement import EngagementDecision

        updates["direct_decision"] = EngagementDecision(
            should_engage=True,
            message=str(direct_next["question"]).strip(),
            file_document_id=None,
            crm_actions=[],
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            next_requirement_id=str(direct_next.get("id") or "") or None,
            model="deterministic",
        )
    return updates


def _route_turn(state: EngagementGraphState) -> dict:
    if state.get("direct_decision") is not None:
        return {"route": "direct"}

    latest = " ".join(str(state.get("latest_text") or "").casefold().split())
    if latest and any(re.fullmatch(pattern, latest) for pattern in _OPT_OUT_PATTERNS):
        from apps.ai_engagement.services.engagement import EngagementDecision

        return {
            "route": "direct",
            "direct_decision": EngagementDecision(
                should_engage=False,
                message="",
                file_document_id=None,
                crm_actions=[],
                reason="OPT_OUT",
                reason_code="OPT_OUT",
                next_requirement_id=None,
                model="deterministic",
            ),
        }

    if state.get("caller_supplied_context"):
        return {"route": "generate"}

    requested = str(state.get("requested_knowledge_query") or "").strip()
    service = state["service"]
    if requested:
        return {"route": "rag", "retrieval_query": requested}
    if service._should_retrieve_knowledge(context=state["context"]):
        query = service._build_knowledge_query(context=state["context"])
        if query:
            return {"route": "rag", "retrieval_query": query}
    return {"route": "generate"}


def _route_after_classification(state: EngagementGraphState) -> Literal["direct", "rag", "generate"]:
    route = state.get("route") or "generate"
    if route not in {"direct", "rag", "generate"}:
        return "generate"
    return route  # type: ignore[return-value]


def _retrieve_knowledge(state: EngagementGraphState) -> dict:
    service = state["service"]
    context = service.context_builder.build(
        organization=state["organization"],
        lead=state["lead"],
        knowledge_query=state.get("retrieval_query") or None,
        message_limit=service.MESSAGE_LIMIT,
        knowledge_limit=service.KNOWLEDGE_LIMIT,
        note_limit=service.NOTE_LIMIT,
    )
    service._validate_context_scope(
        organization=state["organization"],
        lead=state["lead"],
        context=context,
    )

    before = len(context.knowledge or [])
    threshold = _min_rag_similarity()
    filtered = [
        chunk
        for chunk in (context.knowledge or [])
        if float(chunk.get("similarity") or -1.0) >= threshold
    ]

    org_context = dict(context.organization or {})
    org_context["_runtime_policy"] = state.get("runtime_policy") or {}
    context = replace(context, organization=org_context, knowledge=filtered)

    logger.info(
        "ai_engagement_rag organization=%s lead=%s before=%s after=%s threshold=%.3f",
        getattr(state["organization"], "id", ""),
        getattr(state["lead"], "id", ""),
        before,
        len(filtered),
        threshold,
    )
    return {
        "context": context,
        "rag_retrieved": True,
        "rag_chunks_before_filter": before,
        "rag_chunks_after_filter": len(filtered),
    }


def _generate(state: EngagementGraphState) -> dict:
    # Delegate language generation to the existing service with a prebuilt
    # context. This intentionally disables the legacy service's own RAG/direct
    # routing so LangGraph is the single orchestration authority for the turn.
    decision = state["legacy_engage"](
        state["service"],
        organization=state["organization"],
        lead=state["lead"],
        knowledge_query=None,
        context=state["context"],
    )
    return {"decision": decision}


def _use_direct_decision(state: EngagementGraphState) -> dict:
    return {"decision": state.get("direct_decision")}


def _validate_decision(state: EngagementGraphState) -> dict:
    """Apply deterministic post-generation safety checks.

    Detailed CRM schemas, evidence validation and organization scoping remain in
    the existing service/executor. This node adds graph-level invariants and
    keeps future policy checks out of the language-generation node.
    """

    decision = state.get("decision")
    if decision is None:
        from apps.ai_engagement.services.engagement import EngagementError

        raise EngagementError("LangGraph engagement produced no decision.")

    errors: list[str] = []
    if getattr(decision, "should_engage", False) and not str(
        getattr(decision, "message", "") or ""
    ).strip():
        errors.append("empty_customer_message")

    allowed_next = (state.get("qualification_state") or {}).get("next_requirement_id")
    selected_next = getattr(decision, "next_requirement_id", None)
    # The legacy service already validates model-proposed next_requirement after
    # projecting evidence-backed updates. Keep this diagnostic non-destructive,
    # because a valid update in the current turn can legitimately change NEXT.
    if selected_next and selected_next == allowed_next:
        pass

    if errors:
        from apps.ai_engagement.services.engagement import EngagementError

        raise EngagementError("; ".join(errors))
    return {"validation_errors": errors}


def build_engagement_graph():
    builder = StateGraph(EngagementGraphState)
    builder.add_node("prepare", _prepare)
    builder.add_node("deterministic_extract", _deterministic_extract)
    builder.add_node("route_turn", _route_turn)
    builder.add_node("retrieve_knowledge", _retrieve_knowledge)
    builder.add_node("generate", _generate)
    builder.add_node("direct", _use_direct_decision)
    builder.add_node("validate", _validate_decision)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "deterministic_extract")
    builder.add_edge("deterministic_extract", "route_turn")
    builder.add_conditional_edges(
        "route_turn",
        _route_after_classification,
        {
            "direct": "direct",
            "rag": "retrieve_knowledge",
            "generate": "generate",
        },
    )
    builder.add_edge("retrieve_knowledge", "generate")
    builder.add_edge("generate", "validate")
    builder.add_edge("direct", "validate")
    builder.add_edge("validate", END)
    return builder.compile()


ENGAGEMENT_GRAPH = build_engagement_graph()


def run_engagement_graph(
    *,
    service,
    legacy_engage,
    organization,
    lead,
    knowledge_query: str | None = None,
    context=None,
):
    final = ENGAGEMENT_GRAPH.invoke(
        {
            "service": service,
            "legacy_engage": legacy_engage,
            "organization": organization,
            "lead": lead,
            "requested_knowledge_query": str(knowledge_query or ""),
            "supplied_context": context,
        }
    )
    return final["decision"]
