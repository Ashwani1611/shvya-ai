"""Evidence-bound semantic criteria receipts for the background qualifier.

No model call is made by the CRM executor. It can only consume a signed receipt
for the current playbook, lead state and persisted inbound evidence.
"""
from __future__ import annotations

import hashlib
import json
import re

from django.core import signing
from django.db import transaction

from apps.ai_engagement.services.confidentiality import is_sensitive_field_name, safe_attribute_values
from apps.ai_engagement.services.trace_sanitizer import redact_text

RECEIPT_KEY = "_shvya_ai_semantic_criteria"
RECEIPT_SALT = "shvya.playbook.criteria.v1"
RECEIPT_MAX_AGE = 86400
MAX_CRITERIA = 40
MAX_EVIDENCE = 100


def criteria_clauses(ai_playbook: str) -> list[dict]:
    """Keep each authored condition; split conjunctions rather than dropping one."""
    from apps.ai_engagement.services.playbook import parse_playbook
    text = parse_playbook(ai_playbook)["qualification_criteria"]
    clauses = []
    for line in re.split(r"[\n;]+|(?<=\.)\s+", text):
        line = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", line).strip()
        if not line or re.fullmatch(r"(?:the )?lead (?:is )?qualified (?:only )?(?:if|when):?", line, re.I):
            continue
        for clause in re.split(r"\s+and\s+", line, flags=re.I):
            clause = clause.strip().strip(".")
            if clause:
                clauses.append({"id": f"criterion_{len(clauses) + 1}", "text": clause, "source_rule": line})
    return clauses


def _answer_snapshot(state: dict) -> dict:
    return {
        str(key): {field: value.get(field) for field in ("status", "value", "source_message_id")}
        for key, value in (state.get("requirement_states") or {}).items()
        if isinstance(value, dict)
    }


def _acknowledgment_message_id(lead, ai_playbook: str) -> str | None:
    from apps.ai_engagement.services.playbook import parse_playbook
    template = parse_playbook(ai_playbook)["acknowledgment_message"]
    template = re.sub(r"</?[a-z_]+>", "", template, flags=re.I)
    # Match all fixed template parts, permitting only configured name variables.
    parts = [" ".join(part.casefold().split()) for part in re.split(r"\{[^{}]+\}|\[[A-Za-z_ ]+\]", template)]
    parts = [part for part in parts if len(part) >= 5]
    if not parts:
        return None
    messages = lead.whatsapp_messages.filter(
        organization_id=lead.organization_id, direction="outbound",
        status__in=["sent", "delivered", "read"],
    ).order_by("-created_at", "-id").values("id", "body")[:100]
    for message in messages:
        body = " ".join(str(message["body"] or "").casefold().split())
        if all(part in body for part in parts):
            return str(message["id"])
    return None


def _snapshot(*, lead, ai_playbook: str, requirements: list, state: dict) -> dict:
    answers = _answer_snapshot(state)
    source_ids = {str(item["source_message_id"]) for item in answers.values() if item.get("source_message_id")}
    messages = list(lead.whatsapp_messages.filter(
        organization_id=lead.organization_id, direction="inbound", id__in=source_ids,
    ).order_by("created_at", "id").values("id", "body")[:MAX_EVIDENCE])
    sources = {str(item["id"]): str(item["body"] or "") for item in messages}
    latest = lead.whatsapp_messages.filter(
        organization_id=lead.organization_id, direction="inbound",
    ).order_by("-created_at", "-id").values("id", "body").first()
    return {
        "organization_id": str(lead.organization_id), "lead_id": str(lead.id),
        "pipeline_id": str(lead.pipeline_id), "stage_id": str(lead.stage_id),
        "ai_enabled": bool(lead.ai_enabled), "ai_playbook": ai_playbook,
        "requirements": requirements, "answers": answers, "sources": sources,
        "latest_inbound": {"id": str(latest["id"]), "body": latest["body"]} if latest else None,
        "acknowledgment_message_id": _acknowledgment_message_id(lead, ai_playbook),
    }


def snapshot_fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _known_verdict(clause: dict, snapshot: dict) -> str | None:
    from apps.ai_engagement.services.playbook import evaluate_playbook_criteria
    text = clause["text"]
    if re.search(r"\b(?:or|unless|except)\b", text, re.I):
        return "unknown"  # Do not flatten alternatives/exceptions into an AND rule.
    if re.search(r"acknowledg(?:e)?ment", text, re.I):
        if re.search(r"\b(?:not|never|without)\b", text, re.I):
            return "unknown"
        return "pass" if snapshot.get("acknowledgment_message_id") else "unknown"
    # A lead quote is not execution evidence for these external actions.
    if re.search(r"\b(?:sent|delivered|booked|paid|payment|deposit|signed|purchased)\b", text, re.I):
        return "unknown"
    result = evaluate_playbook_criteria(
        "## Qualification Criteria\n" + text,
        requirements=snapshot["requirements"], state={"requirement_states": snapshot["answers"]},
    )
    rules = result.get("rules") or []
    if rules and all(rule.get("verdict") == "pass" for rule in rules):
        return "pass"
    if any(rule.get("verdict") == "fail" for rule in rules):
        return "fail"
    return None


def validate_evaluations(payload: dict, *, clauses: list, snapshot: dict) -> list[dict]:
    """A verdict needs all clauses and exact, current, tenant-owned lead evidence."""
    from apps.ai_engagement.services.qualification_check import QualificationCheckError
    if not isinstance(payload, dict) or set(payload) != {"evaluations"}:
        raise QualificationCheckError("Semantic criteria returned an invalid schema.")
    evaluations = payload["evaluations"]
    if not isinstance(evaluations, list) or len(evaluations) != len(clauses):
        raise QualificationCheckError("Every qualification criterion requires an evaluation.")
    by_id = {}
    for item in evaluations:
        if not isinstance(item, dict) or set(item) != {"criterion_id", "verdict", "evidence"}:
            raise QualificationCheckError("Invalid criterion evidence schema.")
        identifier = item["criterion_id"]
        if not isinstance(identifier, str) or identifier in by_id:
            raise QualificationCheckError("Invalid or duplicate criterion identifier.")
        if item["verdict"] not in {"pass", "fail", "unknown"} or not isinstance(item["evidence"], list):
            raise QualificationCheckError("Invalid criterion verdict.")
        by_id[identifier] = item
    if set(by_id) != {item["id"] for item in clauses}:
        raise QualificationCheckError("Criterion identifiers differ from the authored playbook.")
    required_ids = {str(item.get("id")) for item in snapshot["requirements"]}
    results = []
    for clause in clauses:
        item = by_id[clause["id"]]
        evidence = item["evidence"]
        if len(evidence) > MAX_EVIDENCE:
            raise QualificationCheckError("Too much criterion evidence.")
        for quote in evidence:
            if not isinstance(quote, dict) or set(quote) != {"requirement_id", "source_message_id", "quote"}:
                raise QualificationCheckError("Invalid criterion evidence reference.")
            identifier = quote["requirement_id"]
            if not isinstance(identifier, str):
                raise QualificationCheckError("Invalid requirement identifier.")
            answer = snapshot["answers"].get(identifier, {})
            source_id = quote["source_message_id"]
            text = quote["quote"]
            if (not isinstance(identifier, str) or identifier not in required_ids
                    or answer.get("status") != "answered"
                    or not isinstance(source_id, str) or str(answer.get("source_message_id") or "") != source_id
                    or not isinstance(text, str) or not text.strip()
                    or text not in snapshot["sources"].get(source_id, "")
                    or redact_text(text) != text):
                raise QualificationCheckError("Criterion evidence is not a verified current inbound answer.")
        known = _known_verdict(clause, snapshot)
        verdict = known if known is not None else item["verdict"]
        if verdict == "pass" and known is None and not evidence:
            raise QualificationCheckError("A semantic pass requires inbound evidence.")
        if verdict == "pass" and known is None and any(re.search(
            r"\b(?:ignore (?:all |the |previous )?(?:instructions|rules)|mark (?:me|this lead|the lead|this criterion) (?:as )?(?:qualified|pass)|system\s*:|developer\s*:)",
            quote["quote"], re.I,
        ) for quote in evidence):
            raise QualificationCheckError("Instructions in lead text cannot establish qualification evidence.")
        results.append({"rule": clause["text"], "verdict": verdict, "evidence": evidence})
    return results


def verified_semantic_criteria_for_lead(lead, ai_playbook, requirements, state) -> dict | None:
    attributes = lead.attributes if isinstance(lead.attributes, dict) else {}
    receipt = attributes.get(RECEIPT_KEY)
    if not isinstance(receipt, str):
        return None
    try:
        payload = signing.loads(receipt, salt=RECEIPT_SALT, max_age=RECEIPT_MAX_AGE)
    except signing.BadSignature:
        return None
    snapshot = _snapshot(lead=lead, ai_playbook=ai_playbook, requirements=requirements, state=state)
    if not isinstance(payload, dict) or payload.get("fingerprint") != snapshot_fingerprint(snapshot):
        return None
    evaluations = payload.get("evaluations")
    clauses = criteria_clauses(ai_playbook)
    from apps.ai_engagement.services.qualification_check import QualificationCheckError
    try:
        rules = validate_evaluations(evaluations, clauses=clauses, snapshot=snapshot)
    except (QualificationCheckError, ValueError, TypeError, KeyError):
        return None
    if not rules or any(rule["verdict"] != "pass" for rule in rules):
        return {"qualified": False, "reason": "semantic_criteria_unresolved", "rules": rules}
    return {"qualified": True, "reason": "verified_semantic_criteria", "rules": rules}


def refresh_semantic_criteria(*, service, organization, lead) -> dict:
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.playbook import evaluate_playbook_criteria
    from apps.ai_engagement.services.qualification_state import normalize_stage_name, state_for_lead
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn
    from apps.ai_engagement.services.qualification_check import QualificationCheckError
    from apps.crm.models import Lead

    if lead.organization_id != organization.id:
        raise QualificationCheckError("Lead does not belong to this organization.")
    if not lead.ai_enabled or normalize_stage_name(lead.stage.name) != "new lead":
        return {"status": "skipped", "reason": "qualification_not_active"}
    info = OrgInfo.objects.filter(organization=organization).only("ai_playbook").first()
    raw = info.ai_playbook if info else ""
    requirements = _requirements_for_turn(organization=organization, lead=lead)
    state = state_for_lead(lead, requirements=requirements)
    # Criteria-only playbooks can qualify from real CRM facts without a model.
    # Semantic inference still needs completed, source-bound question answers.
    values = {**(lead.attributes if isinstance(lead.attributes, dict) else {}),
              "name": lead.name, "phone": lead.phone}
    deterministic = evaluate_playbook_criteria(raw, requirements=requirements, state=state, values=values)
    if not requirements:
        return {"status": "verified" if deterministic.get("qualified") else "unresolved",
                "reason": "deterministic_criteria" if deterministic.get("qualified") else "criteria_only_evidence_unresolved"}
    if state.get("qualification_status") != "completed":
        return {"status": "skipped", "reason": "collection_incomplete"}
    if deterministic.get("qualified"):
        return {"status": "verified", "reason": "deterministic_criteria"}
    if any(rule.get("verdict") == "fail" for rule in deterministic.get("rules", [])):
        return {"status": "unresolved", "reason": "qualification_criteria_not_satisfied"}
    clauses = criteria_clauses(raw)
    if not clauses or len(clauses) > MAX_CRITERIA:
        return {"status": "unresolved", "reason": "criteria_missing_or_too_large"}
    snapshot = _snapshot(lead=lead, ai_playbook=raw, requirements=requirements, state=state)
    if verified_semantic_criteria_for_lead(lead, raw, requirements, state) is not None:
        return {"status": "unchanged", "reason": "current_receipt"}
    evidence = []
    for requirement in requirements:
        identifier = str(requirement.get("id"))
        answer = snapshot["answers"].get(identifier, {})
        source_id = str(answer.get("source_message_id") or "")
        body = snapshot["sources"].get(source_id, "")
        if answer.get("status") != "answered" or not body or is_sensitive_field_name(identifier):
            continue
        evidence.append({"requirement_id": identifier, "question": requirement.get("question") or requirement.get("label"),
                         "value": safe_attribute_values({"value": answer.get("value")}).get("value"), "source_message_id": source_id, "body": redact_text(body)[:3000]})
    if not evidence:
        return {"status": "unresolved", "reason": "verified_answers_missing"}
    evaluations = service.evaluate_criteria(clauses=clauses, evidence=evidence,
        organization=organization, lead=lead,
        backend_verdicts={clause["id"]: _known_verdict(clause, snapshot) for clause in clauses})
    rules = validate_evaluations(evaluations, clauses=clauses, snapshot=snapshot)
    fingerprint = snapshot_fingerprint(snapshot)
    with transaction.atomic():
        locked = Lead.objects.select_for_update().select_related("organization", "pipeline", "stage").get(id=lead.id, organization=organization)
        current_info = OrgInfo.objects.filter(organization=organization).only("ai_playbook").first()
        current_raw = current_info.ai_playbook if current_info else ""
        current_requirements = _requirements_for_turn(organization=organization, lead=locked)
        current_state = state_for_lead(locked, requirements=current_requirements)
        current = _snapshot(lead=locked, ai_playbook=current_raw, requirements=current_requirements, state=current_state)
        if snapshot_fingerprint(current) != fingerprint:
            return {"status": "stale", "reason": "qualification_state_changed"}
        locked.attributes = dict(locked.attributes or {})
        locked.attributes[RECEIPT_KEY] = signing.dumps({"fingerprint": fingerprint, "evaluations": evaluations}, salt=RECEIPT_SALT, compress=True)
        locked.save(update_fields=["attributes", "updated_at"])
        lead.attributes = locked.attributes
    return {"status": "verified" if all(item["verdict"] == "pass" for item in rules) else "unresolved",
            "reason": "semantic_criteria_evaluated", "rules": [{"rule": item["rule"], "verdict": item["verdict"]} for item in rules]}


def apply_verified_semantic_completion(*, organization, lead) -> dict:
    """Use the ordinary CRM planner/executor after background verification."""
    from apps.ai_engagement.services.qualification_state import normalize_stage_name, state_for_lead
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn
    from apps.ai_engagement.services.qualification_execution_contract import _config, _completion_target
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor, CRMActionExecutionError
    if lead.organization_id != organization.id or not lead.ai_enabled or normalize_stage_name(lead.stage.name) != "new lead":
        return {"status": "skipped", "reason": "qualification_not_active"}
    requirements = _requirements_for_turn(organization=organization, lead=lead)
    state = state_for_lead(lead, requirements=requirements)
    if requirements and state.get("qualification_status") != "completed":
        return {"status": "skipped", "reason": "collection_incomplete"}
    config = _config(organization=organization, requirements=requirements)
    target = _completion_target(lead=lead, state=state, config=config)
    if not target:
        return {"status": "unresolved", "reason": "criteria_or_target_unresolved"}
    source = lead.whatsapp_messages.filter(organization=organization, direction="inbound").order_by("-created_at", "-id").first()
    if source is None:
        return {"status": "unresolved", "reason": "inbound_evidence_missing"}
    try:
        results = CRMActionExecutor().execute(organization=organization, lead=lead, source_message=source,
            actions=[{"type": "pipeline_transition", "stage_shift": {"stage_id": str(target["id"])}}])
    except CRMActionExecutionError:
        return {"status": "blocked", "reason": "completion_action_not_authorized"}
    return {"status": "executed", "results": results}
