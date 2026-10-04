from __future__ import annotations

import logging
import os
import re
from copy import deepcopy
from dataclasses import replace
from typing import Literal

from langgraph.graph import END, START, StateGraph

from apps.ai_engagement.graph.evidence import check_grounding, select_chunks
from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.graph.runtime_policy import get_runtime_policy
from apps.ai_engagement.graph.state import EngagementGraphState
from apps.ai_engagement.services.evidence_recovery import (
    assess_evidence, enabled as evidence_recovery_enabled, recovery_route,
    retrieve_evidence, retry_retrieval,
)
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
    requires_response_composition,
)
from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION,
    REQUIREMENT_ANSWERED,
    apply_unambiguous_reply,
    requirements_for_lead,
    state_for_lead,
)
from apps.ai_engagement.services.runtime_state import (
    STATE_KEY,
    contract,
    observe_message,
    state_revision,
    validate_response,
)


logger = logging.getLogger(__name__)


def _file_trace(**fields):
    """Keep draft/final counters separate; never log customer or file content."""
    from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY
    from apps.ai_engagement.services.trace_service import record
    phase = "final" if _FINAL_LANGUAGE_ONLY.get() else "draft"
    record("file_decision", {phase: fields})


def _welcome_due_for_context(*, decision, context, lead):
    """Match the first-reply welcome boundary without inferring file permission."""
    if not decision.should_engage or getattr(decision, "reason_code", "") == "ANSWER_ORG_QUESTION":
        return False
    from apps.crm.models import Lead
    if isinstance(lead, Lead):
        from apps.ai_engagement.services.first_inbound_welcome_runtime import _is_first_inbound_turn
        # Live context can be scoped to one account or omit empty bodies. The
        # canonical welcome predicate checks all persisted lead messages.
        return _is_first_inbound_turn(lead)
    conversation = context.conversation or {}
    messages = [item for item in conversation.get("messages", []) if isinstance(item, dict)]
    return bool(
        conversation.get("message_count", len(messages)) == 1
        and len(messages) == 1 and messages[0].get("direction") == "inbound"
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
    from apps.ai_engagement.services.turn_controller import (
        build_turn_policy,
        record_turn_policy,
    )
    turn_policy = build_turn_policy(
        context=context,
        qualification_state=state_for_lead(lead, requirements=requirements),
    )
    record_turn_policy(policy=turn_policy)

    org_context = dict(context.organization or {})
    org_context["_runtime_policy"] = policy
    context = replace(context, organization=org_context)
    qualification_state = state_for_lead(lead, requirements=requirements)
    lead_context = dict(context.lead or {})
    attributes = dict(lead_context.get("attributes") or {})
    attributes[STATE_KEY] = observe_message(
        attributes.get(STATE_KEY),
        service._latest_inbound_text(context=context),
    )
    lead_context["attributes"] = attributes
    context = replace(context, lead=lead_context)

    return {
        "context": context,
        "profile": policy_profile,
        "runtime_policy": policy,
        "turn_policy": turn_policy,
        "requirements": requirements,
        "qualification_state": qualification_state,
        "latest_text": service._latest_inbound_text(context=context),
        "latest_message_id": service._latest_inbound_message_id(context=context),
        "caller_supplied_context": caller_supplied,
        "validation_errors": [],
    }


def _canonical_yes_no_reply(state: EngagementGraphState, requirements: list[dict]) -> str:
    """Normalize natural yes/no wording only for an active Yes/No option question.

    The qualification state machine deliberately accepts option values exactly.
    Sandbox users also commonly answer a binary question with text such as
    "YES, I RUN ADS". Treat that as the authored Yes option without broadening
    matching for arbitrary multi-option questions.
    """
    raw_text = str(state.get("latest_text") or "").strip()
    normalized = " ".join(raw_text.casefold().split())
    if not normalized:
        return raw_text

    affirmative = bool(re.match(r"^(?:yes|yeah|yep)\b", normalized))
    negative = bool(re.match(r"^(?:no|nope)\b", normalized))
    if not affirmative and not negative:
        return raw_text

    qualification_state = state.get("qualification_state") or {}
    active_id = str(
        qualification_state.get("current_requirement_id")
        or qualification_state.get("last_asked_requirement_id")
        or ""
    ).strip()
    if not active_id:
        return raw_text

    requirement = next(
        (
            item
            for item in requirements
            if str(item.get("id") or "").strip() == active_id
        ),
        None,
    )
    if not isinstance(requirement, dict):
        return raw_text

    options = [item for item in requirement.get("options") or [] if isinstance(item, dict)]
    if len(options) != 2:
        return raw_text

    by_value = {
        str(item.get("value") or "").strip().casefold(): str(item.get("value") or "").strip()
        for item in options
        if str(item.get("value") or "").strip()
    }
    if set(by_value) != {"yes", "no"}:
        return raw_text

    return by_value["yes"] if affirmative else by_value["no"]


def _deterministic_extract(state: EngagementGraphState, *, reply_text: str | None = None) -> dict:
    """Handle high-confidence replies only against the persisted active question."""
    if state.get("caller_supplied_context"):
        return {}

    lead = state["lead"]
    requirements = state.get("requirements") or []
    if not requirements:
        return {}

    direct_text = _canonical_yes_no_reply(state, requirements) if reply_text is None else reply_text
    direct = apply_unambiguous_reply(
        lead=lead,
        requirements=requirements,
        text=direct_text,
        source_message_id=state.get("latest_message_id", ""),
    )
    qualification_state = direct["state"]
    updates: dict = {
        "qualification_state": qualification_state,
        "answer_extracted": bool(direct.get("changed") and direct.get("answer_status") == REQUIREMENT_ANSWERED),
    }

    direct_next = direct.get("next_requirement")

    # Keep high-confidence answer extraction deterministic, but let the normal
    # response path apply language, Playbook actions and guided-file conditions.
    # Knowing the next question does not prove that raw English copy is a valid
    # complete response for this organization.
    if (
        direct.get("changed")
        and direct.get("answer_status") == REQUIREMENT_ANSWERED
        and qualification_state.get("engagement_mode") == MODE_QUALIFICATION
        and isinstance(direct_next, dict)
        and direct_next.get("can_direct_ask")
        and str(direct_next.get("question") or "").strip()
    ):
        from apps.ai_engagement.services.engagement import EngagementDecision

        context = state["context"]
        if (requires_response_composition(context.organization or {}, profile=state.get("profile"))
                or (context.pipeline or {}).get("attribute_definitions")):
            return updates
        context = _with_file_candidates(state, context)
        updates["context"] = context
        if (context.organization or {}).get("_file_candidates"):
            return updates
        next_question = str(direct_next["question"]).strip()
        updates["direct_decision"] = EngagementDecision(
            should_engage=True,
            message=next_question,
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
    if state.get("answer_extracted"):
        # A validated short answer such as "Referrals" needs Playbook/language
        # composition, not an embedding search for the customer's own fact.
        return {"route": "generate"}
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
    if evidence_recovery_enabled(state):
        return retrieve_evidence(state)
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
        org_context["_file_candidates"] = file_candidates[:10]
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


def _with_file_candidates(state: EngagementGraphState, context):
    """Resolve eligible files before selecting a response-generation shortcut."""
    from apps.ai_engagement.services.file_sharing import FileSharingService
    from apps.organizations.models import Organization
    candidates = (context.organization or {}).get("_file_candidates")
    if candidates is not None:
        return context
    # Pure policy previews may use synthetic organizations; they have no
    # document store. Preserve their authored context without synthetic fields.
    organization = state["organization"]
    if not isinstance(organization, Organization) or not organization.pk:
        return context
    candidates = FileSharingService().build_file_candidates(
        organization=organization, context=context,
    )
    return replace(context, organization={**context.organization, "_file_candidates": candidates})


def _build_business_plan(state: EngagementGraphState) -> dict:
    from apps.ai_engagement.services.turn_controller import (
        build_business_plan,
        record_turn_policy,
    )

    context = _with_file_candidates(state, state["context"])
    turn_policy = state.get("turn_policy")
    if turn_policy is None:
        from apps.ai_engagement.services.turn_controller import build_turn_policy
        turn_policy = build_turn_policy(
            context=context,
            qualification_state=state.get("qualification_state") or {},
        )
    plan = build_business_plan(
        service=state["service"],
        context=context,
        qualification_state=state.get("qualification_state") or {},
        requirements=state.get("requirements") or [],
        latest_text=state.get("latest_text", ""),
        turn_policy=turn_policy,
    )
    org_context = dict(context.organization or {})
    org_context["_business_plan"] = plan
    context = replace(context, organization=org_context)
    record_turn_policy(policy=turn_policy, business_plan=plan)
    return {"context": context, "business_plan": plan}


def _generate(state: EngagementGraphState) -> dict:
    # Keep the authored organization policy intact. The service uses the same
    # persisted flow snapshot as this graph; serializing questions back to prose
    # destroys explicit IDs, conditional rules and flow-version metadata.
    context = _with_file_candidates(state, state["context"])
    # FAQs are authored AI Brain facts, not dependent on vector indexing or
    # English lexical overlap. Keep complete Q/A pairs for semantic validation.
    from apps.organizations.models import Organization
    if isinstance(state["organization"], Organization):
        from apps.ai_engagement.services.authored_knowledge import authored_answer_candidates
        try:
            faqs = authored_answer_candidates(
                organization=state["organization"], question=state.get("latest_text", ""),
            )
            context = replace(context, organization={
                **context.organization, "_authored_faq_candidates": faqs,
            })
        except Exception:
            logger.exception("AI Brain FAQ context unavailable organization=%s", state["organization"].pk)

    decision = state["legacy_engage"](
        state["service"],
        organization=state["organization"],
        lead=state["lead"],
        knowledge_query=None,
        context=context,
    )
    from apps.ai_engagement.services.qualification_capture_recovery import recover_omitted_answers
    decision = recover_omitted_answers(
        service=state["service"], organization=state["organization"], lead=state["lead"],
        context=context, decision=decision, requirements=state.get("requirements") or [],
        qualification_state=state.get("qualification_state") or {},
        model_override=getattr(state.get("turn_policy"), "model_override", ""),
    )
    # A draft can omit a requested file or authored welcome attachment. Review
    # only that missing choice, retaining the reply, qualification evidence and
    # CRM proposals; the authored file condition remains the sharing authority.
    from apps.ai_engagement.services.file_sharing import FileSharingService, explicit_file_request
    from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY
    candidates = (context.organization or {}).get("_file_candidates") or []
    requested = explicit_file_request(state.get("latest_text", ""))
    welcome_due = _welcome_due_for_context(decision=decision, context=context, lead=state["lead"])
    review_trigger = "explicit_request" if requested else "welcome" if welcome_due else "none"
    review_status = (
        "final_language_only" if _FINAL_LANGUAGE_ONLY.get()
        else "already_selected" if decision.file_document_id is not None
        else "silenced" if not decision.should_engage
        else "not_requested" if review_trigger == "none"
        else "no_candidates" if not candidates
        else "pending"
    )
    _file_trace(candidate_count=len(candidates), explicit_request=requested, welcome_due=welcome_due,
                review_trigger=review_trigger,
                draft_selected_count=int(decision.file_document_id is not None))
    if (decision.should_engage and decision.file_document_id is None and candidates
            and not _FINAL_LANGUAGE_ONLY.get()
            and review_trigger != "none"):
        from apps.ai_engagement.services.ai_provider import OpenAIProvider
        try:
            provider = state["service"].provider or OpenAIProvider(timeout_seconds=20)
            if isinstance(provider, OpenAIProvider):
                from copy import copy
                # Preserve organization/model configuration and the injected
                # client, while bounding this optional call independently.
                provider = copy(provider)
                provider.client = provider.client.with_options(timeout=20)
            selected = FileSharingService().review_requested_file(
                organization=state["organization"], lead=state["lead"], context=context,
                candidates=candidates, provider=provider,
                generate=state["service"]._generate_provider_text,
                model_override=getattr(state.get("turn_policy"), "model_override", ""),
                welcome_due=welcome_due,
            )
            decision = replace(decision, file_document_id=selected)
            review_status = "selected" if selected is not None else "declined"
        except Exception:
            # A failed optional review must not invent a send, discard captured
            # answers or turn a valid customer reply into a provider error.
            logger.exception("Requested file review failed organization=%s", state["organization"].id)
            review_status = "failed"
    _file_trace(review_status=review_status, selected_count=int(decision.file_document_id is not None))
    return {"decision": decision, "context": context, "welcome_due": welcome_due}


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
    _file_trace(validated_selected_count=int(validated_file_id is not None),
                validation_drop=int(decision.file_document_id is not None and validated_file_id is None),
                proposed_capture_count=len(decision.qualification_updates or []))
    from apps.ai_engagement.services.qualification_state import project_answer_updates

    projected = project_answer_updates(
        state=state.get("qualification_state") or {},
        requirements=state.get("requirements") or [],
        updates=decision.qualification_updates,
        messages=(state["context"].conversation or {}).get("messages", []),
    )
    runtime = contract(
        qualification=projected,
        requirements=state.get("requirements") or [],
        saved=observe_message(
            ((state["context"].lead or {}).get("attributes") or {}).get(STATE_KEY),
            state.get("latest_text", ""),
        ),
    )
    validate_response(
        decision=decision,
        runtime=runtime,
        requirements=state.get("requirements") or [],
    )
    decision = replace(
        decision,
        crm_actions=controlled_actions,
        file_document_id=validated_file_id,
        backend_revision=state_revision(state["lead"]),
        flow_version=runtime["flow_version"],
    )

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
    return {
        "decision": decision,
        "validation_errors": errors,
        "qualification_state": projected,
    }


def build_engagement_graph():
    builder = StateGraph(EngagementGraphState)
    builder.add_node("prepare", _prepare)
    builder.add_node("deterministic_extract", _deterministic_extract)
    builder.add_node("route_turn", _route_turn)
    builder.add_node("retrieve_knowledge", _retrieve_knowledge)
    builder.add_node("assess_evidence", assess_evidence)
    builder.add_node("retry_retrieval", retry_retrieval)
    builder.add_node("reassess_evidence", assess_evidence)
    builder.add_node("build_business_plan", _build_business_plan)
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
        {"direct": "direct", "rag": "retrieve_knowledge", "generate": "assess_evidence"},
    )
    builder.add_edge("retrieve_knowledge", "assess_evidence")
    builder.add_conditional_edges(
        "assess_evidence", recovery_route,
        {"generate": "build_business_plan", "retry_retrieval": "retry_retrieval"},
    )
    builder.add_edge("retry_retrieval", "reassess_evidence")
    builder.add_edge("reassess_evidence", "build_business_plan")
    builder.add_edge("build_business_plan", "generate")
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
    from time import monotonic
    final = ENGAGEMENT_GRAPH.invoke(
        {
            "started_at": monotonic(),
            "service": service,
            "legacy_engage": legacy_engage,
            "organization": organization,
            "lead": lead,
            "requested_knowledge_query": str(knowledge_query or ""),
            "supplied_context": context,
        }
    )
    _file_trace(graph_selected_count=int(final["decision"].file_document_id is not None),
                graph_capture_count=len(final["decision"].qualification_updates or []),
                grounding_status="approved" if final.get("grounding_approved") else "rejected")
    return final["decision"]
