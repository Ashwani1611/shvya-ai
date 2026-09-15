from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import replace
from functools import wraps
from typing import Any

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
_INSTALLED = False
_CONTRACT_KEY = "qualification_execution_contract"
_PLAN_KEY = "qualification_response_plan"

_ACK_LABEL = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<label>(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message|final\s+acknowledg(?:e)?ment)\s*(?::|=|->|→)\s*(?P<value>.+?)\s*$",
    re.I,
)
_LABEL_ONLY = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message|final\s+acknowledg(?:e)?ment|qualification\s+requirements?|attribute\s+mapped|stage\s+shifting)\s*:?\s*$",
    re.I,
)
_COMPLETION_RULE = re.compile(
    r"\b(?:qualification\s+(?:is\s+)?(?:complete|completed)|(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?(?:questions?|requirements?|answers?)\s+(?:are\s+)?(?:answered|complete|completed)|(?:after|once|when)\s+(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?(?:questions?|requirements?)\s+(?:are\s+)?(?:answered|complete|completed))\b",
    re.I,
)
_GENERIC_ACKS = {"got it", "great", "nice", "okay", "ok", "noted", "thanks", "thank you"}

_PLAN_INSTRUCTIONS = """
BACKEND RESPONSE PLAN CONTRACT
- response_plan is backend-authoritative when present.
- qualification_progress: write one short personalized acknowledgement reflecting acknowledgement_context. Do not rewrite the next question/options; backend appends them.
- qualification_complete: write one short personalized acknowledgement reflecting the final answer. Backend appends the configured final acknowledgement value.
- Never expose configuration labels, requirement ids, attribute keys, stage ids, or execution metadata.
""".strip()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: Any) -> str:
    return _clean(value).casefold()


def _strip_quotes(value: str) -> str:
    value = str(value or "").strip()
    return value[1:-1].strip() if len(value) > 1 and value[0] == value[-1] and value[0] in {"'", '"'} else value


def _processing(message) -> dict[str, Any]:
    payload = message.raw_payload if message and isinstance(message.raw_payload, dict) else {}
    data = payload.get("shvya_ai_processing")
    return deepcopy(data) if isinstance(data, dict) else {}


def _save_processing(message, processing: dict[str, Any]) -> None:
    payload = deepcopy(message.raw_payload) if isinstance(message.raw_payload, dict) else {}
    payload["shvya_ai_processing"] = deepcopy(processing)
    message.raw_payload = payload
    message.save(update_fields=["raw_payload", "updated_at"])


def _reference(value: Any) -> str:
    text = _norm(value).strip("`'\"[](){} ")
    return re.sub(r"^(?:requirement|question|attribute|field)\s+", "", text).strip()


def _requirement_ref(value: str, requirements: list[dict[str, Any]]) -> dict[str, Any] | None:
    ref = _reference(value)
    match = re.fullmatch(r"q(?:uestion)?\s*(\d+)", ref)
    if match:
        found = [r for r in requirements if int(r.get("priority") or 0) == int(match.group(1))]
        return found[0] if len(found) == 1 else None
    found = []
    for req in requirements:
        aliases = {
            _reference(req.get("id")),
            _reference(req.get("stable_id")),
            _reference(req.get("label")),
            _reference(str(req.get("question") or "").splitlines()[0]),
            *(_reference(v) for v in req.get("legacy_ids") or []),
        }
        aliases.discard("")
        if ref and ref in aliases:
            found.append(req)
    return found[0] if len(found) == 1 else None


def _attribute_ref(value: str, definitions: list[dict[str, Any]]) -> dict[str, Any] | None:
    ref = _reference(value)
    found = [
        item for item in definitions
        if ref and ref in {_reference(item.get("key")), _reference(item.get("name"))}
    ]
    return found[0] if len(found) == 1 else None


def _split_mapping(line: str) -> tuple[str, str] | None:
    text = str(line or "").strip()
    parts = re.split(r"\s*(?:->|=>|→)\s*", text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    match = re.match(r"^\s*map\s+(.+?)\s+to\s+(.+?)\s*$", text, re.I)
    if match:
        return match.group(1), match.group(2)
    match = re.match(r"^\s*(.+?)\s+maps?\s+to\s+(.+?)\s*$", text, re.I)
    return (match.group(1), match.group(2)) if match else None


def _config(*, organization, requirements: list[dict[str, Any]]) -> dict[str, Any]:
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement_instruction_policy import section_lines
    from apps.crm.models import AttributeDefinition, Stage

    info = OrgInfo.objects.filter(organization=organization).first()
    raw = str(getattr(info, "engagement_instructions", "") or "")
    definitions = list(
        AttributeDefinition.objects.filter(organization=organization)
        .values("key", "name", "field_type", "description", "options")
    )

    mappings: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    for line in section_lines(raw, "attribute_mapped"):
        pair = _split_mapping(line)
        if not pair:
            errors.append({"type": "configuration_error", "status": "failed", "code": "invalid_attribute_mapping_rule", "detail": line})
            continue
        req = _requirement_ref(pair[0], requirements)
        attr = _attribute_ref(pair[1], definitions)
        if req is None:
            errors.append({"type": "configuration_error", "status": "failed", "code": "unknown_requirement_mapping_reference", "detail": pair[0]})
            continue
        if attr is None:
            errors.append({"type": "configuration_error", "status": "failed", "code": "unknown_attribute_mapping_reference", "detail": pair[1]})
            continue
        req_id, key = str(req.get("id") or ""), str(attr.get("key") or "")
        if req_id in mappings and mappings[req_id] != key:
            errors.append({"type": "configuration_error", "status": "failed", "code": "ambiguous_attribute_mapping", "detail": req_id})
        else:
            mappings[req_id] = key

    ack_values: list[str] = []
    for line in raw.splitlines():
        match = _ACK_LABEL.match(line.strip())
        if not match:
            continue
        value = _strip_quotes(match.group("value"))
        if value and not _LABEL_ONLY.match(value):
            ack_values.append(value)
        else:
            errors.append({"type": "configuration_error", "status": "failed", "code": "invalid_final_acknowledgement_value", "detail": match.group("label")})
    ack_values = list(dict.fromkeys(ack_values))
    final_ack = ack_values[0] if len(ack_values) == 1 else None
    if len(ack_values) > 1:
        errors.append({"type": "configuration_error", "status": "failed", "code": "ambiguous_final_acknowledgement", "detail": "Multiple values configured."})

    stages = list(
        Stage.objects.filter(pipeline__organization=organization, pipeline__is_active=True, is_active=True)
        .values("id", "name", "pipeline_id", "pipeline__name")
    )
    stage_targets = []
    for line in section_lines(raw, "stage_shifting"):
        if not _COMPLETION_RULE.search(line):
            continue
        normalized = _norm(line)
        matches = [
            stage for stage in stages
            if _norm(stage["name"]) and re.search(rf"(?<![a-z0-9]){re.escape(_norm(stage['name']))}(?![a-z0-9])", normalized)
        ]
        if len(matches) > 1:
            matches = [s for s in matches if _norm(s["pipeline__name"]) and _norm(s["pipeline__name"]) in normalized]
        if len(matches) == 1:
            stage_targets.append(matches[0])
        else:
            errors.append({"type": "configuration_error", "status": "failed", "code": "unresolved_completion_stage_rule", "detail": line})
    unique = {str(s["id"]): s for s in stage_targets}
    completion_stage = next(iter(unique.values())) if len(unique) == 1 else None
    if len(unique) > 1:
        errors.append({"type": "configuration_error", "status": "failed", "code": "ambiguous_completion_stage_rule", "detail": "Multiple completion targets configured."})

    return {"mappings": mappings, "final_ack": final_ack, "completion_stage": completion_stage, "errors": errors}


def _render_requirement(requirement: dict[str, Any] | None) -> str:
    if not isinstance(requirement, dict):
        return ""
    question = str(requirement.get("question") or requirement.get("label") or "").strip()
    base = question.splitlines()[0].strip()
    options = [x for x in requirement.get("options") or [] if isinstance(x, dict) and str(x.get("value") or "").strip()]
    if not options:
        return question
    return base + "\n" + "\n".join(f"{str(x.get('key') or '').strip()}. {str(x.get('value') or '').strip()}" for x in options)


def _plan(*, source, answer, state, requirements, config, results) -> dict[str, Any]:
    from apps.ai_engagement.services.qualification_state import next_requirement

    completed = _norm(state.get("qualification_status")) == "completed"
    if completed:
        stage_result = next((r for r in results if isinstance(r, dict) and r.get("type") == "pipeline_transition"), None)
        target = config.get("completion_stage")
        return {
            "response_type": "qualification_complete",
            "qualification_complete": True,
            "acknowledgement_required": True,
            "acknowledgement_context": {"raw_answer": str(source.body or ""), "normalized_answer": answer},
            "final_configured_acknowledgement": {"value": config.get("final_ack")},
            "execution_results": {
                "attributes": [r for r in results if isinstance(r, dict) and r.get("type") == "attribute_updates"],
                "stage_transition": {
                    "configured": bool(target),
                    "target_stage_id": str(target["id"]) if target else None,
                    "target_pipeline_id": str(target["pipeline_id"]) if target else None,
                    "result": deepcopy(stage_result),
                },
                "other_actions": [r for r in results if isinstance(r, dict) and r.get("type") not in {"attribute_updates", "pipeline_transition"}],
            },
        }
    nxt = next_requirement(requirements, state.get("requirement_states") or {})
    return {
        "response_type": "qualification_progress",
        "qualification_complete": False,
        "acknowledgement_required": True,
        "acknowledgement_context": {"raw_answer": str(source.body or ""), "normalized_answer": answer},
        "next_requirement": (
            {"id": str(nxt.get("id") or ""), "question": str(nxt.get("question") or "").splitlines()[0].strip(), "options": deepcopy(nxt.get("options") or []), "rendered": _render_requirement(nxt)}
            if isinstance(nxt, dict) else None
        ),
        "execution_results": {"attributes": [r for r in results if isinstance(r, dict) and r.get("type") == "attribute_updates"], "stage_transition": None, "other_actions": []},
    }


def _verify_attribute(lead, key, expected) -> dict[str, Any]:
    lead.refresh_from_db(fields=["attributes"])
    actual = (lead.attributes or {}).get(key)
    ok = actual == expected
    return {"type": "attribute_updates", "status": "executed" if ok else "failed", "verified": ok, "updates": [{"key": key, "expected": expected, "actual": actual}]}


def _verify_stage(lead, target) -> dict[str, Any]:
    lead.refresh_from_db(fields=["pipeline", "stage"])
    actual = str(lead.stage_id or "")
    ok = actual == str(target["id"])
    return {
        "type": "pipeline_transition",
        "status": "executed" if ok else "failed",
        "verified": ok,
        "target_stage_id": str(target["id"]),
        "target_pipeline_id": str(target["pipeline_id"]),
        "actual_stage_id": actual,
        "actual_pipeline_id": str(lead.pipeline_id or ""),
    }


def resolve_before_generation(*, organization, lead, source_message_id, account_id=None) -> dict[str, Any]:
    """Resolve a high-confidence active qualification answer and dependent CRM work before response generation."""
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.canonical_architecture import StateReconciler
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor
    from apps.ai_engagement.services.transactional_turn_runtime import (
        _mark_state_resolved,
        _persist_updates_against_requirements,
        _requirements_for_turn,
    )
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead

    with transaction.atomic():
        locked = Lead.objects.select_for_update().select_related("organization", "pipeline", "stage").get(pk=lead.pk, organization=organization)
        query = WhatsAppMessage.objects.select_for_update().filter(pk=source_message_id, organization=organization, lead=locked, direction=WhatsAppMessage.Direction.INBOUND)
        if account_id is not None:
            query = query.filter(account_id=account_id)
        source = query.first()
        if source is None:
            return {"applied": False, "reason": "source_message_not_found"}
        processing = _processing(source)
        if isinstance(processing.get(_CONTRACT_KEY), dict) and processing[_CONTRACT_KEY].get("applied"):
            return {"applied": False, "reason": "already_applied"}

        requirements = _requirements_for_turn(organization=organization, lead=locked)
        state = qs.state_for_lead(locked, requirements=requirements)
        if not requirements or _norm(state.get("qualification_status")) == "completed":
            return {"applied": False, "reason": "no_active_qualification"}
        active_id = str(state.get("current_requirement_id") or state.get("last_asked_requirement_id") or "")
        requirement = next((r for r in requirements if str(r.get("id") or "") == active_id), None)
        if requirement is None:
            return {"applied": False, "reason": "no_active_requirement"}

        classified = qs._classify_direct_reply(text=source.body, question=str(requirement.get("question") or requirement.get("label") or ""))
        if classified is None or classified[0] != qs.REQUIREMENT_ANSWERED:
            return {"applied": False, "reason": "requires_llm_interpretation"}
        _, answer, confidence = classified
        update = {"requirement_id": active_id, "value": answer, "source_message_id": str(source.id), "evidence": str(source.body or "").strip()}

        projected = qs.project_answer_updates(
            state=state,
            requirements=requirements,
            updates=[update],
            messages=[{"id": str(source.id), "body": source.body, "direction": "inbound"}],
        )
        config = _config(organization=organization, requirements=requirements)

        # Build every dependent action while the active requirement and answer are still explicit.
        attr_key = config["mappings"].get(active_id)
        attr_action = {"type": "attribute_updates", "updates": [{"key": attr_key, "value": answer}]} if attr_key else None
        mapping_error = any(e["code"] in {"invalid_attribute_mapping_rule", "unknown_requirement_mapping_reference", "unknown_attribute_mapping_reference", "ambiguous_attribute_mapping"} for e in config["errors"])
        completed = _norm(projected.get("qualification_status")) == "completed"
        target = config.get("completion_stage")
        stage_action = {"type": "pipeline_transition", "stage_shift": {"stage_id": str(target["id"])}} if completed and target and not mapping_error else None

        results = deepcopy(config["errors"])
        action_types = ["qualification_state"]
        _persist_updates_against_requirements(lead=locked, requirements=requirements, updates=[update])
        locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])

        attr_ok = True
        if attr_action:
            try:
                CRMActionExecutor().execute(organization=organization, lead=locked, actions=[attr_action])
                verified = _verify_attribute(locked, attr_key, answer)
                results.append(verified)
                attr_ok = bool(verified["verified"])
                if attr_ok:
                    action_types.append("attribute_updates")
            except Exception as exc:
                attr_ok = False
                results.append({"type": "attribute_updates", "status": "failed", "code": "attribute_persistence_failed", "detail": str(exc)[:500]})

        if stage_action and attr_ok and not mapping_error:
            try:
                CRMActionExecutor().execute(organization=organization, lead=locked, actions=[stage_action])
                verified = _verify_stage(locked, target)
                results.append(verified)
                if verified["verified"]:
                    action_types.append("pipeline_transition")
            except Exception as exc:
                results.append({"type": "pipeline_transition", "status": "failed", "code": "stage_transition_failed", "detail": str(exc)[:500]})

        locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        final_state = qs.state_for_lead(locked, requirements=requirements)
        plan = _plan(source=source, answer=answer, state=final_state, requirements=requirements, config=config, results=results)

        _mark_state_resolved(lead=locked, inbound=source, action_types=action_types)
        source.refresh_from_db(fields=["raw_payload"])
        processing = _processing(source)
        processing[_CONTRACT_KEY] = {
            "applied": True,
            "applied_at": timezone.now().isoformat(),
            "requirement_id": active_id,
            "normalized_answer": answer,
            "confidence": confidence,
            "configuration_errors": deepcopy(config["errors"]),
        }
        processing[_PLAN_KEY] = deepcopy(plan)
        _save_processing(source, processing)

        reconciler = StateReconciler()
        snapshot = reconciler.build(
            lead=locked,
            source_message_id=source.id,
            execution_results=results,
            structured_decision={
                "intent": "qualification_answer",
                "source_message_id": str(source.id),
                "qualification_updates": [update],
                "attribute_updates": deepcopy(attr_action["updates"]) if attr_action else [],
                "workflow_actions": [deepcopy(stage_action)] if stage_action else [],
            },
        )
        snapshot["response_plan"] = deepcopy(plan)
        reconciler.persist_for_source(lead=locked, source_message_id=source.id, snapshot=snapshot)
        return {"applied": True, "qualification_status": final_state.get("qualification_status"), "response_plan": plan, "execution_results": results, "stage_id": str(locked.stage_id or "")}


def _plan_from_reconciled(*, lead, source_message_id, snapshot):
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn

    requirements = _requirements_for_turn(organization=lead.organization, lead=lead)
    source = lead.whatsapp_messages.filter(pk=source_message_id, direction="inbound").only("body").first()
    if source is None or not requirements:
        return None
    state = qs.state_for_lead(lead, requirements=requirements)
    answered = [
        item for item in (state.get("requirement_states") or {}).values()
        if isinstance(item, dict)
        and str(item.get("source_message_id") or "") == str(source_message_id)
        and _norm(item.get("status")) == "answered"
    ]
    if not answered:
        return None
    config = _config(organization=lead.organization, requirements=requirements)
    results = deepcopy(snapshot.get("execution_results") or [])
    target = config.get("completion_stage")
    if target:
        for i, result in enumerate(results):
            if isinstance(result, dict) and result.get("type") == "pipeline_transition":
                results[i] = _verify_stage(lead, target)
                break
    return _plan(source=source, answer=answered[-1].get("value"), state=state, requirements=requirements, config=config, results=results)


def _ack_from_message(message: str, plan: dict[str, Any]) -> str:
    text = str(message or "").strip()
    final_value = str(((plan.get("final_configured_acknowledgement") or {}).get("value") or "")).strip()
    next_rendered = str(((plan.get("next_requirement") or {}).get("rendered") or "")).strip()
    if final_value:
        text = text.replace(final_value, " ")
    if next_rendered:
        text = text.replace(next_rendered, " ")

    next_question = _norm((plan.get("next_requirement") or {}).get("question"))
    option_values = {_norm(x.get("value")) for x in ((plan.get("next_requirement") or {}).get("options") or []) if isinstance(x, dict)}
    kept = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _ACK_LABEL.match(line)
        if match:
            line = _strip_quotes(match.group("value"))
        if _LABEL_ONLY.match(line):
            continue
        normalized = _norm(line)
        if next_question and normalized == next_question:
            continue
        if option_values and re.match(r"^[a-z0-9][).:\-]\s+", normalized) and any(normalized.endswith(v) for v in option_values):
            continue
        kept.append(line)
    ack = " ".join(kept).strip()
    return "" if _norm(ack).strip(" .!?") in _GENERIC_ACKS else ack


def _stage_success(state: dict[str, Any]) -> bool:
    plan = state.get("response_plan") if isinstance(state, dict) else {}
    execution = plan.get("execution_results") if isinstance(plan, dict) else {}
    stage_info = execution.get("stage_transition") if isinstance(execution, dict) else {}
    result = stage_info.get("result") if isinstance(stage_info, dict) else {}
    target = str(stage_info.get("target_stage_id") or "") if isinstance(stage_info, dict) else ""
    stage = state.get("stage") if isinstance(state, dict) else {}
    return bool(
        stage_info and stage_info.get("configured") and target
        and isinstance(stage, dict) and str(stage.get("id") or "") == target
        and isinstance(result, dict) and result.get("verified") is True
        and _norm(result.get("status")) == "executed"
    )


def _finalize(decision, state):
    from apps.ai_engagement.services import canonical_architecture as canonical
    from apps.ai_engagement.services.engagement import EngagementError

    plan = state.get("response_plan") if isinstance(state, dict) else None
    if not isinstance(plan, dict):
        return decision
    kind = str(plan.get("response_type") or "")
    if kind not in {"qualification_progress", "qualification_complete", "qualification_clarification"}:
        return decision

    ack = _ack_from_message(getattr(decision, "message", ""), plan)
    if plan.get("acknowledgement_required") and not ack:
        raise EngagementError("Qualification response requires a personalized acknowledgement.")

    if kind in {"qualification_progress", "qualification_clarification"}:
        rendered = str(((plan.get("next_requirement") or {}).get("rendered") or "")).strip()
        if not rendered:
            raise EngagementError("Qualification response plan is missing the next configured requirement.")
        message = f"{ack}\n\n{rendered}".strip()
    else:
        final_value = str(((plan.get("final_configured_acknowledgement") or {}).get("value") or "")).strip()
        message = f"{ack}\n\n{final_value}".strip() if final_value else ack

    if canonical._QUALIFIED_CLAIM_RE.search(message) and not _stage_success(state):
        message = canonical.ResponseActionValidator._replace_sentence(message, canonical._QUALIFIED_CLAIM_RE, "")

    cleaned = []
    for raw in message.splitlines():
        line = raw.strip()
        if not line:
            cleaned.append("")
            continue
        match = _ACK_LABEL.match(line)
        if match:
            value = _strip_quotes(match.group("value"))
            if value:
                cleaned.append(value)
            continue
        if not _LABEL_ONLY.match(line):
            cleaned.append(line)
    return replace(decision, message="\n".join(cleaned).strip())


def _latest_inbound(lead, account_id=None):
    query = lead.whatsapp_messages.filter(direction="inbound")
    if account_id is not None:
        query = query.filter(account_id=account_id)
    return query.order_by("-created_at", "-id").first()


def install_qualification_execution_contract() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService
    current_input = EngagementService._build_input
    current_instructions = EngagementService._build_instructions

    @wraps(current_input)
    def build_input(self, *, context, **kwargs):
        raw = current_input(self, context=context, **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
        reconciled = payload.get("reconciled_state")
        if isinstance(reconciled, dict) and isinstance(reconciled.get("response_plan"), dict):
            payload["response_plan"] = deepcopy(reconciled["response_plan"])
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @wraps(current_instructions)
    def build_instructions(self, *, context, profile=None):
        base = current_instructions(self, context=context, profile=profile)
        return base if _PLAN_INSTRUCTIONS in base else f"{base}\n\n{_PLAN_INSTRUCTIONS}"

    EngagementService._build_input = build_input
    EngagementService._build_instructions = build_instructions

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.canonical_architecture import ResponseActionValidator, StateReconciler
    current_resolve = runtime._resolve_state_before_response
    reconciler = StateReconciler()

    @wraps(current_resolve)
    def resolve(*, organization, lead, source_message_id, decision, account_id=None):
        result = current_resolve(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            decision=decision,
            account_id=account_id,
        )
        snapshot = result.get("reconciled_state") if isinstance(result, dict) else None
        if not result or not result.get("applied") or not isinstance(snapshot, dict):
            return result
        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        plan = _plan_from_reconciled(lead=lead, source_message_id=source_message_id, snapshot=snapshot)
        if not plan:
            return result
        snapshot = {**snapshot, "response_plan": plan}
        reconciler.persist_for_source(lead=lead, source_message_id=source_message_id, snapshot=snapshot)
        return {**result, "reconciled_state": snapshot}

    runtime._resolve_state_before_response = resolve

    current_validate = ResponseActionValidator.validate

    @wraps(current_validate)
    def validate(self, *, decision, reconciled_state):
        validated = current_validate(self, decision=decision, reconciled_state=reconciled_state)
        return _finalize(validated, reconciled_state if isinstance(reconciled_state, dict) else {})

    ResponseActionValidator.validate = validate

    from apps.ai_engagement import tasks as task_module
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.crm.models import Lead
    from services.channels.whatsapp_service import resolve_account_for_lead

    current_task = task_module._execute_ai_engagement_response_impl

    @wraps(current_task)
    def execute(*, task, lead_id: str):
        lead = Lead.objects.select_related("organization", "pipeline", "stage").filter(pk=lead_id).first()
        if lead is not None:
            source = _latest_inbound(lead)
            try:
                permission = AIPermissionService().evaluate(organization=lead.organization, lead=lead, latest_inbound=source)
            except Exception:
                permission = None
            account = resolve_account_for_lead(organization=lead.organization, lead=lead) if permission and permission.allowed else None
            if source is not None and account is not None:
                try:
                    resolve_before_generation(organization=lead.organization, lead=lead, source_message_id=source.pk)
                except Exception:
                    logger.exception("Qualification pre-generation execution failed for lead %s", lead_id)
        return current_task(task=task, lead_id=lead_id)

    task_module._execute_ai_engagement_response_impl = execute

    from apps.hosted_automation import execution as hosted_execution
    current_hosted = hosted_execution.execute_hosted_ai_engagement

    @wraps(current_hosted)
    def execute_hosted(*, task, job):
        lead = Lead.objects.select_related("organization", "pipeline", "stage").filter(pk=job.lead_id, organization_id=job.organization_id).first()
        if lead is not None:
            source = _latest_inbound(lead, account_id=job.account_id)
            try:
                permission = AIPermissionService().evaluate(organization=lead.organization, lead=lead, latest_inbound=source)
            except Exception:
                permission = None
            account = hosted_execution._connected_hosted_account(account_id=job.account_id, organization_id=job.organization_id)
            if permission and permission.allowed and account is not None and source is not None and str(source.pk) == str(job.source_message_id):
                try:
                    resolve_before_generation(
                        organization=lead.organization,
                        lead=lead,
                        source_message_id=source.pk,
                        account_id=job.account_id,
                    )
                except Exception:
                    logger.exception("Hosted qualification pre-generation execution failed for lead %s", job.lead_id)
        return current_hosted(task=task, job=job)

    hosted_execution.execute_hosted_ai_engagement = execute_hosted
    _INSTALLED = True
