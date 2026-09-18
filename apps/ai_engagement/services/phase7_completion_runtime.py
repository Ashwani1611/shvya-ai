from __future__ import annotations

from functools import wraps
from types import SimpleNamespace
from typing import Any, Mapping


_INSTALLED = False

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


def _intent_values(turn) -> tuple[str, ...]:
    decision = (turn or {}).get("intent_decision")
    primary = str(
        getattr(getattr(decision, "primary_intent", None), "value", "") or ""
    ).strip()
    secondary = [
        str(getattr(item, "value", item) or "").strip()
        for item in (getattr(decision, "secondary_intents", ()) or ())
    ]
    return tuple(item for item in (primary, *secondary) if item)


def _intent_label(turn) -> str:
    return "+".join(_intent_values(turn))


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


def _decision_for_planning(decision, turn):
    """Add a proposal-only safe fallback when structured intent requests a call.

    A call request with a grounded date but no grounded time cannot legally become
    CREATE_REMINDER because inventing a due time would violate the reminder
    schema. In that case Phase 7 still needs a structured proposal boundary, so
    the planner emits its existing non-executing HUMAN_HANDOFF proposal while the
    source intent/policy retain that this was a CALL_REQUEST. The executor sees no
    new mutation action from this fallback.
    """
    intents = set(_intent_values(turn))
    if "CALL_REQUEST" not in intents:
        return decision
    actions = [
        dict(item)
        for item in (getattr(decision, "crm_actions", None) or [])
        if isinstance(item, Mapping)
    ]
    if any(item.get("type") == "create_reminder" for item in actions):
        return decision
    if str(getattr(decision, "reason_code", "") or "").upper() == "HUMAN_HANDOFF":
        return decision
    return SimpleNamespace(
        crm_actions=actions,
        file_document_id=getattr(decision, "file_document_id", None),
        reason_code="HUMAN_HANDOFF",
    )


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
        planning_decision = _decision_for_planning(decision, turn)
        if message is not None and "OPT_OUT" not in _intent_values(turn):
            from apps.ai_engagement.services.sales_intelligence import ObjectionEngine
            from apps.ai_engagement.services.intent_rules import deterministic_intents
            from apps.ai_engagement.services.intent_types import Intent
            if Intent.OPT_OUT not in deterministic_intents(message.body):
                objections = ObjectionEngine().detect(text=message.body, settings=organization.settings,
                                                      intent_decision=(turn or {}).get("intent_decision"))
                if any(item.escalate for item in objections):
                    planning_decision = SimpleNamespace(
                        crm_actions=getattr(planning_decision, "crm_actions", []) or [],
                        file_document_id=getattr(planning_decision, "file_document_id", None),
                        reason_code="HUMAN_HANDOFF")
        return original(
            self,
            organization=organization,
            lead=lead,
            decision=planning_decision,
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


def _deterministically_supported_reply(decision, resolution) -> bool:
    # Compatibility for callers/tests; the one shared grounding guard is
    # installed by Phase 5. Do not stack a second weaker approval predicate.
    from apps.ai_engagement.services.grounding_safety import exact_evidence_reply

    return exact_evidence_reply(decision, resolution)


def install_phase7_completion_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_planner_provenance_bridge()
    _INSTALLED = True


__all__ = [
    "install_phase7_completion_runtime",
    "_decision_for_planning",
    "_deterministically_supported_reply",
]
