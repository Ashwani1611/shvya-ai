from __future__ import annotations

import re
from functools import wraps
from typing import Any, Mapping


_INSTALLED = False
_WORD_RE = re.compile(r"[a-z0-9]+", re.I)
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "for",
    "from",
    "has",
    "have",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "our",
    "the",
    "this",
    "to",
    "we",
    "with",
    "you",
    "your",
}


def _policy_turn(*, organization, lead) -> dict[str, Any] | None:
    try:
        from apps.ai_engagement.services import conversation_policy_runtime as policy_runtime

        turn = policy_runtime._TURN.get()
    except Exception:
        return None
    if not isinstance(turn, dict):
        return None
    if str(turn.get("organization_id") or "") != str(getattr(organization, "id", "")):
        return None
    if str(turn.get("lead_id") or "") != str(getattr(lead, "id", "")):
        return None
    return turn


def _source_message(*, organization, lead, turn, supplied=None):
    if supplied is not None:
        return supplied
    source_id = str((turn or {}).get("source_message_id") or "").strip()
    if not source_id:
        return None
    try:
        return (
            lead.whatsapp_messages.filter(
                pk=source_id,
                organization_id=organization.id,
                direction="inbound",
            )
            .only("id", "body", "organization_id", "lead_id")
            .first()
        )
    except Exception:
        return None


def _intent_label(turn) -> str:
    decision = (turn or {}).get("intent_decision")
    primary = getattr(getattr(decision, "primary_intent", None), "value", "")
    secondary = [
        str(getattr(item, "value", item) or "").strip()
        for item in (getattr(decision, "secondary_intents", ()) or ())
    ]
    values = [str(primary or "").strip(), *secondary]
    return "+".join(item for item in values if item)


def _policy_values(turn) -> tuple[str, str]:
    if not turn:
        return "", ""
    try:
        from apps.ai_engagement.services import conversation_policy_runtime as policy_runtime

        policy = policy_runtime._POLICY.get()
    except Exception:
        policy = None
    if policy is None:
        return "", ""
    outcome = getattr(getattr(policy, "outcome", None), "value", "")
    source = (
        str(getattr(policy, "reason_code", "") or "").strip()
        or str(getattr(policy, "policy_source", "") or "").strip()
    )
    return source, str(outcome or "").strip()


def _trace_evidence_refs() -> list[dict[str, Any]]:
    try:
        from apps.ai_engagement.services.trace_service import current

        buffer = current()
    except Exception:
        buffer = None
    if buffer is None:
        return []
    grounding = buffer.data.get("grounding")
    grounding = grounding if isinstance(grounding, Mapping) else {}
    category = grounding.get("category")
    question_type = grounding.get("question_type")
    refs: list[dict[str, Any]] = []
    for source in grounding.get("sources") or ():
        if not isinstance(source, Mapping):
            continue
        item = {
            "category": category,
            "question_type": question_type,
            "source_id": source.get("source_id"),
            "source_type": source.get("source_type"),
            "score": source.get("score"),
            "metadata": source.get("metadata"),
        }
        refs.append({key: value for key, value in item.items() if value is not None})
        if len(refs) >= 8:
            break
    if not refs and category:
        refs.append(
            {
                "category": category,
                "question_type": question_type,
                "source_type": "backend_evidence_resolver",
            }
        )
    return refs


def _memory_refs(*, organization, lead) -> list[dict[str, Any]]:
    try:
        from apps.ai_engagement.services.structured_memory import StructuredLeadMemoryService

        snapshot = StructuredLeadMemoryService().load(
            organization=organization,
            lead=lead,
        )
    except Exception:
        return []
    facts = snapshot.get("facts") if isinstance(snapshot, Mapping) else {}
    if not isinstance(facts, Mapping):
        return []
    refs: list[dict[str, Any]] = []
    for key, value in facts.items():
        if not isinstance(value, Mapping):
            continue
        item = {
            "key": str(key),
            "confidence": value.get("confidence"),
            "source_message_id": value.get("source_message_id"),
            "source_type": value.get("source_type"),
        }
        refs.append({name: raw for name, raw in item.items() if raw is not None})
        if len(refs) >= 20:
            break
    return refs


def _install_planner_provenance_bridge() -> None:
    from apps.ai_engagement.services.action_planner import ActionPlanner

    original = ActionPlanner.plan

    @wraps(original)
    def plan(
        self,
        *,
        organization,
        lead,
        decision,
        source_message=None,
        source_intent="",
        source_policy="",
        policy_outcome="",
        source_evidence=None,
        source_memory=None,
    ):
        turn = _policy_turn(organization=organization, lead=lead)
        message = _source_message(
            organization=organization,
            lead=lead,
            turn=turn,
            supplied=source_message,
        )
        inferred_policy, inferred_outcome = _policy_values(turn)
        return original(
            self,
            organization=organization,
            lead=lead,
            decision=decision,
            source_message=message,
            source_intent=source_intent or _intent_label(turn),
            source_policy=source_policy or inferred_policy,
            policy_outcome=policy_outcome or inferred_outcome,
            source_evidence=(
                source_evidence
                if source_evidence is not None
                else _trace_evidence_refs()
            ),
            source_memory=(
                source_memory
                if source_memory is not None
                else _memory_refs(organization=organization, lead=lead)
            ),
        )

    ActionPlanner.plan = plan


def _normalized_tokens(value: Any) -> set[str]:
    return {
        token.casefold()
        for token in _WORD_RE.findall(str(value or ""))
        if len(token) > 2 and token.casefold() not in _STOP_WORDS
    }


def _deterministically_supported_reply(decision, resolution) -> bool:
    if resolution is None or not getattr(resolution, "verified", False):
        return False
    if getattr(decision, "crm_actions", None) or getattr(decision, "qualification_updates", None):
        return False
    if getattr(decision, "file_document_id", None) is not None:
        return False
    question_type = str(getattr(resolution, "question_type", "") or "")
    if question_type not in {
        "pricing",
        "policy",
        "location",
        "working_hours",
        "product_or_service",
    }:
        return False
    contents = [
        str(getattr(item, "content", "") or "").strip()
        for item in (getattr(resolution, "evidence", ()) or ())
        if str(getattr(item, "content", "") or "").strip()
    ]
    if not contents:
        return False
    reply_tokens = _normalized_tokens(getattr(decision, "message", ""))
    if not reply_tokens:
        return False
    evidence_tokens: set[str] = set()
    for content in contents:
        evidence_tokens.update(_normalized_tokens(content))
    if not evidence_tokens:
        return False
    # Deterministic bypass is intentionally strict. Every meaningful reply token
    # must already occur in same-tenant verified evidence. Any paraphrase or
    # unsupported claim remains on the existing verifier path.
    return reply_tokens.issubset(evidence_tokens)


def _install_grounding_budget_guard() -> None:
    from apps.ai_engagement.graph import evidence as evidence_graph
    from apps.ai_engagement.graph import workflow as workflow

    original = evidence_graph.check_grounding

    @wraps(original)
    def check_grounding(state):
        decision = state.get("decision")
        if decision is None or not getattr(decision, "should_engage", False):
            return original(state)
        try:
            from apps.ai_engagement.services.phase5_6_runtime import (
                current_evidence_resolution,
            )

            resolution = current_evidence_resolution(
                organization_id=getattr(state.get("organization"), "id", None),
                lead_id=getattr(state.get("lead"), "id", None),
            )
        except Exception:
            resolution = None
        if _deterministically_supported_reply(decision, resolution):
            return {
                "grounding_approved": True,
                "grounding_validation_path": "deterministic_verified_evidence",
            }
        return original(state)

    evidence_graph.check_grounding = check_grounding
    workflow.check_grounding = check_grounding
    # LangGraph captures node callables when compiled, so rebind the one existing
    # canonical graph after replacing the evidence node. This is not a new graph.
    workflow.ENGAGEMENT_GRAPH = workflow.build_engagement_graph()


def install_phase7_completion_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_planner_provenance_bridge()
    _install_grounding_budget_guard()
    _INSTALLED = True


__all__ = [
    "install_phase7_completion_runtime",
    "_deterministically_supported_reply",
]
