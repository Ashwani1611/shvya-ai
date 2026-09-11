from __future__ import annotations

import logging
import os
from copy import deepcopy
from dataclasses import replace
from typing import Literal

from langgraph.graph import END, START, StateGraph

from apps.ai_engagement.graph.evidence import check_grounding, select_chunks
from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.graph.runtime_policy import get_runtime_policy
from apps.ai_engagement.graph.state import EngagementGraphState
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
)
from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION,
    REQUIREMENT_ANSWERED,
    apply_unambiguous_reply,
    requirements_for_lead,
    state_for_lead,
)


logger = logging.getLogger(__name__)


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

    service._validate_context_scope(organization=organization, lead=lead, context=context)
    profile = compile_org_ai_profile_from_context(context.organization or {})
    configured_requirements = profile.get("qualification", {}).get("requirements", [])
    requirements = requirements_for_lead(lead, configured_requirements)

    # An in-progress lead is pinned to the flow snapshot that began the
    # conversation. The deterministic evaluator must use the same version as
    # state persistence, even if an admin edits AI Setup midway through the chat.
    policy_profile = deepcopy(profile)
    policy_qualification = policy_profile.setdefault("qualification", {})
    policy_qualification["requirements"] = deepcopy(requirements)
    if requirements:
        policy_qualification["flow_version"] = str(
            requirements[0].get("flow_version") or policy_qualification.get("flow_version") or ""
        )
    policy = get_runtime_policy(organization=organization, profile=policy_profile)

    org_context = dict(context.organization or {})
    org_context["_runtime_policy"] = policy
    context = replace(context, organization=org_context)
    qualification_state = state_for_lead(lead, requirements=requirements)

    return {
        "context": context,
        "profile": policy_profile,
        "runtime_policy": policy,
        "requirements": requirements,
        "qualification_state": qualification_state,
        "latest_text": service._latest_inbound_text(context=context),
        "latest_message_id": service._latest_inbound_message_id(context=context),
        "caller_supplied_context": caller_supplied,
        "validation_errors": [],
    }


def _deterministic_extract(state: EngagementGraphState) -> dict:
    """Handle high-confidence replies only against the persisted active question."""
    if state.get("caller_supplied_context"):
        return {}

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
        and not profile.get("qualification", {}).get("raw")
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

    if state.get("caller_supplied_context"):
        context = state["context"]
        return {
            "route": "generate",
            "context": replace(
                context,
                knowledge=select_chunks(
                    context.knowledge,
                    threshold=_min_rag_similarity(),
                    limit=state["service"].KNOWLEDGE_LIMIT,
                ),
            ),
        }

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
    organization = state["organization"]
    context = service.context_builder.build(
        organization=organization,
        lead=state["lead"],
        knowledge_query=state.get("retrieval_query") or None,
        message_limit=service.MESSAGE_LIMIT,
        knowledge_limit=service.KNOWLEDGE_LIMIT,
        note_limit=service.NOTE_LIMIT,
    )
    service._validate_context_scope(organization=organization, lead=state["lead"], context=context)

    before = len(context.knowledge or [])
    threshold = _min_rag_similarity()
    filtered = select_chunks(context.knowledge, threshold=threshold, limit=service.KNOWLEDGE_LIMIT)
    # Preserve the state-authoritative organization wrapper created in prepare;
    # rebuilding context for RAG must not reintroduce a different qualification version.
    context = replace(state["context"], knowledge=filtered)

    from apps.ai_engagement.services.file_sharing import FileSharingService
    file_candidates = FileSharingService().build_file_candidates(
        organization=organization,
        context=context,
    )

    org_context = dict(context.organization or {})
    org_context["_runtime_policy"] = state.get("runtime_policy") or {}
    if file_candidates:
        org_context["_file_candidates"] = file_candidates[: service.KNOWLEDGE_LIMIT]
    context = replace(context, organization=org_context)

    logger.info(
        "ai_engagement_rag organization=%s lead=%s before=%s after=%s file_candidates=%s threshold=%.3f",
        getattr(organization, "id", ""),
        getattr(state["lead"], "id", ""),
        before,
        len(filtered),
        len(file_candidates),
        threshold,
    )
    return {
        "context": context,
        "rag_retrieved": True,
        "rag_chunks_before_filter": before,
        "rag_chunks_after_filter": len(filtered),
    }


def _requirements_as_authoring_text(requirements: list[dict]) -> str:
    """Serialize only the pinned flow so legacy compilation cannot see new edits."""
    lines: list[str] = []
    for requirement in requirements or []:
        question = str(requirement.get("question") or requirement.get("label") or "").strip()
        if question:
            lines.append(question)
    return "\n".join(lines)


def _generate(state: EngagementGraphState) -> dict:
    context = state["context"]
    requirements = state.get("requirements") or []
    if requirements:
        org_context = dict(context.organization or {})
        org_context["qualification_requirements"] = _requirements_as_authoring_text(requirements)
        context = replace(context, organization=org_context)

    decision = state["legacy_engage"](
        state["service"],
        organization=state["organization"],
        lead=state["lead"],
        knowledge_query=None,
        context=context,
    )
    return {"decision": decision}


def _use_direct_decision(state: EngagementGraphState) -> dict:
    return {"decision": state.get("direct_decision")}


def _validated_file_document_id(*, decision, context) -> int | None:
    selected = getattr(decision, "file_document_id", None)
    if selected is None:
        return None
    try:
        selected_id = int(selected)
    except (TypeError, ValueError):
        return None
    organization_context = context.organization if isinstance(context.organization, dict) else {}
    candidates = organization_context.get("_file_candidates")
    if not isinstance(candidates, list):
        return None
    allowed = {
        int(item["document_id"])
        for item in candidates
        if isinstance(item, dict) and item.get("document_id") is not None
    }
    return selected_id if selected_id in allowed else None


def _validate_decision(state: EngagementGraphState) -> dict:
    """Validate language output, then let Python own CRM/action authorization."""
    decision = state.get("decision")
    if decision is None:
        from apps.ai_engagement.services.engagement import EngagementError
        raise EngagementError("LangGraph engagement produced no decision.")

    errors: list[str] = []
    if getattr(decision, "should_engage", False) and not str(getattr(decision, "message", "") or "").strip():
        errors.append("empty_customer_message")
    if errors:
        from apps.ai_engagement.services.engagement import EngagementError
        raise EngagementError("; ".join(errors))

    controlled_actions, policy_result = build_controlled_actions(
        decision=decision,
        context=state["context"],
        runtime_policy=state.get("runtime_policy") or {},
        qualification_state=state.get("qualification_state") or {},
        requirements=state.get("requirements") or [],
    )
    validated_file_id = _validated_file_document_id(decision=decision, context=state["context"])
    decision = replace(decision, crm_actions=controlled_actions, file_document_id=validated_file_id)

    logger.info(
        "ai_engagement_graph organization=%s lead=%s route=%s model=%s rag=%s/%s qualification=%s crm_actions=%s file=%s",
        getattr(state["organization"], "id", ""),
        getattr(state["lead"], "id", ""),
        state.get("route") or "generate",
        getattr(decision, "model", ""),
        state.get("rag_chunks_after_filter", 0),
        state.get("rag_chunks_before_filter", 0),
        (policy_result.get("evaluation") or {}).get("outcome"),
        len(controlled_actions),
        validated_file_id,
    )
    return {"decision": decision, "validation_errors": errors}


def build_engagement_graph():
    builder = StateGraph(EngagementGraphState)
    builder.add_node("prepare", _prepare)
    builder.add_node("deterministic_extract", _deterministic_extract)
    builder.add_node("route_turn", _route_turn)
    builder.add_node("retrieve_knowledge", _retrieve_knowledge)
    builder.add_node("generate", _generate)
    builder.add_node("direct", _use_direct_decision)
    builder.add_node("validate", _validate_decision)
    builder.add_node("grounding", check_grounding)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "deterministic_extract")
    builder.add_edge("deterministic_extract", "route_turn")
    builder.add_conditional_edges(
        "route_turn",
        _route_after_classification,
        {"direct": "direct", "rag": "retrieve_knowledge", "generate": "generate"},
    )
    builder.add_edge("retrieve_knowledge", "generate")
    builder.add_edge("generate", "validate")
    builder.add_edge("direct", "validate")
    builder.add_edge("validate", "grounding")
    builder.add_edge("grounding", END)
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
    final = ENGAGEMENT_GRAPH.invoke({
        "service": service,
        "legacy_engage": legacy_engage,
        "organization": organization,
        "lead": lead,
        "requested_knowledge_query": str(knowledge_query or ""),
        "supplied_context": context,
    })
    return final["decision"]
