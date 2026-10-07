"""Billable, stateful tests using production AI/CRM services and owned fixtures.

No channel sender, message queue, workflow or calendar action is invoked. These
are real persisted CRM/AI state tests, not proof of live provider delivery.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.models import InternalConversationSummary, OrgInfo, FAQ, Document, KnowledgeSource, Chunk
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import CAP_DIAGNOSTICS_READ
from apps.integrations.operations_testing_models import OperationsAIFlowRun, OperationsAIFlowTurn
from apps.integrations.operations_testing_scope import owned_test_scope
from apps.integrations.operations_testing_budget import budget_usage
from apps.integrations.operations_tools import (
    OperationsToolError, ToolExecution, _organization_for, _require_operations_capability,
    _write_gate, _uuid, _proposal_digest, _ensure_approved_proposal_unchanged,
    _reject_secret_like_content,
)

CAP_AI_FLOW_TEST_WRITE = "ai.flow_testing.write"
MODE = "production_ai_and_crm_isolated_no_delivery"
LIMITATIONS = [
    "Tests shared production AI decisions, extraction, CRM attributes/stages and persisted summaries.",
    "Does not test provider webhooks, delivery, queues, channel permissions, cadence or workflow dispatch.",
    "Files are selected only; reminders, calendar bookings and external actions are not executed.",
    "Instagram's native message adapter is not covered by this WhatsApp fixture harness.",
]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _configuration(organization):
    info = OrgInfo.objects.filter(organization=organization).values(
        "about", "bot_languages", "ai_playbook", "qualification_model", "sales_support_model", "summary_model", "ai_enabled"
    ).first()
    return _digest({"info": info,
        "organization": organization.__class__.objects.filter(pk=organization.pk).values("name", "timezone", "settings").first(),
        "faqs": list(FAQ.objects.filter(organization=organization).order_by("id").values("id", "updated_at", "is_active", "question", "answer")),
        "documents": list(Document.objects.filter(organization=organization).order_by("id").values("id", "updated_at", "is_active", "file_sharing_ready", "processing_status")),
        "knowledge_sources": list(KnowledgeSource.objects.filter(organization=organization).order_by("id").values("id", "updated_at", "is_active")),
        "knowledge_chunks": list(Chunk.objects.filter(organization=organization).order_by("id").values("id", "document_id", "updated_at", "is_active")),
        "pipelines": list(Pipeline.objects.filter(organization=organization).order_by("id").values("id", "name", "description", "ai_enabled", "is_active")),
        "stages": list(Stage.objects.filter(pipeline__organization=organization).order_by("id").values("id", "name", "description", "config", "ai_on", "is_active")),
        "attributes": list(AttributeDefinition.objects.filter(organization=organization).order_by("id").values()),
    })


def _run(organization, arguments, *, lock=False):
    query = OperationsAIFlowRun.objects.select_for_update() if lock else OperationsAIFlowRun.objects
    item = query.filter(pk=_uuid((arguments or {}).get("run_id"), field="run_id"), organization=organization).first()
    if item is None:
        raise OperationsToolError("AI flow test run not found in this organization.")
    return item


def _integer(arguments, name, default, maximum):
    value = arguments.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise OperationsToolError(f"{name} must be an integer from 1 to {maximum}.")
    return value


def _key(arguments):
    key = str(arguments.get("idempotency_key") or "").strip()
    if not key or len(key) > 100:
        raise OperationsToolError("idempotency_key is required and must be at most 100 characters.")
    return key


def _execution(data, run, reason="", *, dry_run=False, capability=CAP_AI_FLOW_TEST_WRITE, proposal=None):
    return ToolExecution(data={"execution_mode": MODE, "messages_sent": 0, "live_delivery_test": False, **data},
        capability=capability, target_type="ai_flow_test", target_id=str(run.pk) if run else "", reason=reason,
        outcome=OperationsAuditEvent.Outcome.DRY_RUN if dry_run else OperationsAuditEvent.Outcome.SUCCESS,
        audit_summary={"operation": "ai_flow_test", "run_id": str(run.pk) if run else "", "messages_sent": 0,
                       **({"proposal_digest": _proposal_digest(proposal)} if proposal is not None else {})})


def _snapshot(run):
    return {"run_id": str(run.pk), "name": run.name, "status": run.status,
        "fixture_lead_id": str(run.fixture_lead_id), "fixture_account_id": str(run.fixture_account_id),
        "active_turn_id": str(run.active_turn_id) if run.active_turn_id else None,
        "max_turns": run.max_turns, "turn_count": run.turns.count(), "budget": budget_usage(run),
        "configuration_digest": run.configuration_digest, "channel": run.channel,
        "cleanup_complete": run.cleaned_at is not None, "configuration_changed": _configuration(run.organization) != run.configuration_digest,
        "settings_restoration": "No organization, channel or production-lead settings are changed.",
        "limitations": LIMITATIONS}


def _lead_state(run):
    with owned_test_scope(run):
        lead = Lead.objects.filter(pk=run.fixture_lead_id, organization=run.organization, is_operations_test=True).select_related("stage", "pipeline").first()
        if lead is None:
            return None
        from apps.ai_engagement.services.confidentiality import safe_attribute_values
        from apps.ai_engagement.services.qualification_state import state_for_lead
        summary = InternalConversationSummary.objects.filter(organization=run.organization, lead=lead, is_active=True).first()
        return {"lead_id": str(lead.pk), "pipeline_id": str(lead.pipeline_id), "stage_id": str(lead.stage_id),
            "pipeline": lead.pipeline.name, "stage": lead.stage.name,
            "attributes": safe_attribute_values(deepcopy(lead.attributes)),
            "qualification": state_for_lead(lead), "summary": summary.summary if summary else "",
            "summary_id": str(summary.pk) if summary else None,
            "persisted": True, "routable_contact": False}


def create_ai_flow_test_run(*, identity, arguments):
    arguments = arguments or {}
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization,
        capability=CAP_AI_FLOW_TEST_WRITE, tool_name="create_ai_flow_test_run", arguments=arguments)
    if not OrgInfo.objects.filter(organization=organization).exists():
        raise OperationsToolError("Configure and save AI Setup before creating an AI flow test run.")
    key = _key(arguments)
    name = str(arguments.get("name") or "AI flow test").strip()
    if not name or len(name) > 120:
        raise OperationsToolError("name must be 1 to 120 characters.")
    _reject_secret_like_content(name, field="name")
    channel = arguments.get("channel", "whatsapp")
    if channel != "whatsapp":
        raise OperationsToolError("This harness tests the shared WhatsApp AI/CRM pipeline; native Instagram adapter testing is not available.")
    pipeline = Pipeline.objects.filter(pk=_uuid(arguments.get("pipeline_id"), field="pipeline_id"), organization=organization, is_active=True).first()
    stage = Stage.objects.filter(pk=_uuid(arguments.get("stage_id"), field="stage_id"), pipeline=pipeline, is_active=True).first() if pipeline else None
    if stage is None:
        raise OperationsToolError("Choose an active stage in an active pipeline owned by this organization.")
    limits = {"max_turns": _integer(arguments, "max_turns", 30, 500),
        "max_provider_calls": _integer(arguments, "max_provider_calls", 100, 2000),
        "max_credits": _integer(arguments, "max_credits", 1000, 100000)}
    inputs = {"name": name, "channel": channel, "pipeline_id": str(pipeline.pk), "stage_id": str(stage.pk), **limits}
    digest = _digest(inputs)
    existing = OperationsAIFlowRun.objects.filter(organization=organization, idempotency_key=key).first()
    if existing:
        if existing.input_digest != digest:
            raise OperationsToolError("idempotency_key was already used with different test configuration.")
        return _execution({**_snapshot(existing), "idempotent_replay": True}, existing, reason)
    proposal = {**inputs, "configuration_digest": _configuration(organization), "idempotency_key": key,
        "fixture_policy": "server-owned-unroutable-real-lead-and-inactive-account"}
    if dry_run:
        return _execution({"status": "DRY_RUN", "proposal": proposal, "billable_on_turn_execution": True,
            "limitations": LIMITATIONS, "approval_required": True}, None, reason, dry_run=True, proposal=proposal)
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    with transaction.atomic():
        # Organization lock serializes create idempotency without modifying configuration.
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        existing = OperationsAIFlowRun.objects.filter(organization=organization, idempotency_key=key).first()
        if existing:
            if existing.input_digest != digest:
                raise OperationsToolError("idempotency_key was already used with different test configuration.")
            return _execution({**_snapshot(existing), "idempotent_replay": True}, existing, reason)
        run = OperationsAIFlowRun.objects.create(organization=organization, actor=identity.actor,
            fixture_lead_id=uuid.uuid4(), fixture_account_id=uuid.uuid4(), name=name, channel=channel,
            idempotency_key=key, input_digest=digest, configuration_digest=proposal["configuration_digest"], **limits)
        with owned_test_scope(run):
            account = WhatsAppAccount(id=run.fixture_account_id, organization=organization,
                business_name=f"Isolated AI test {run.pk}", is_active=False, status="disconnected", is_operations_test=True)
            account.save()
            # No lead-created signal fanout; no contacts, phone, email or sender.
            Lead.objects.bulk_create([Lead(id=run.fixture_lead_id, organization=organization,
                pipeline=pipeline, stage=stage, name=f"TEST — {name}", phone="", email="",
                is_operations_test=True, ai_enabled=False, auto_followup_enabled=False, lead_source="whatsapp")])
        run.manifest = {"lead_ids": [str(run.fixture_lead_id)], "account_ids": [str(run.fixture_account_id)],
            "pipeline_id": str(pipeline.pk), "initial_stage_id": str(stage.pk), "settings_changed": []}
        run.save(update_fields=["manifest", "updated_at"])
    return _execution({**_snapshot(run), "state": _lead_state(run)}, run, reason)


def get_ai_flow_test_run(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_DIAGNOSTICS_READ)
    run = _run(organization, arguments)
    limit = _integer(arguments or {}, "limit", 50, 100)
    turns = list(run.turns.order_by("-created_at").values("id", "idempotency_key", "message", "status", "result", "created_at")[:limit])
    # JSON-safe shape for MCP and audit serialization.
    turns = json.loads(json.dumps(turns, default=str))
    return _execution({**_snapshot(run), "state": _lead_state(run) if not run.cleaned_at else None, "turns": turns}, run, capability=CAP_DIAGNOSTICS_READ)


def _execute_production_turn(run, turn):
    """Uses real production decision, answer persistence, CRM executor and summary."""
    from apps.ai_engagement.services.turn_controller import TurnController
    from apps.ai_engagement.services.turn_scope import isolated_turn
    from apps.ai_engagement.services.engagement_execution import _persist_engagement_answers
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor
    from apps.ai_engagement.services.internal_summary import InternalSummaryService
    with owned_test_scope(run), isolated_turn():
        if not OrgInfo.objects.filter(organization=run.organization).exists():
            raise OperationsToolError("AI Setup was removed; test execution cannot create shared configuration.")
        lead = Lead.objects.select_related("organization", "pipeline", "stage").get(
            pk=run.fixture_lead_id, organization=run.organization, is_operations_test=True)
        account = WhatsAppAccount.objects.get(pk=run.fixture_account_id, organization=run.organization,
            is_operations_test=True, is_active=False, access_token="", phone_number_id="", display_phone_number="")
        source = WhatsAppMessage(organization=run.organization, account=account, lead=lead,
            direction="inbound", external_id=f"operations-test:{turn.pk}:in", body=turn.message,
            from_number="", to_number="", status="received", raw_payload={"operations_test_run_id": str(run.pk)})
        WhatsAppMessage.objects.bulk_create([source])
        decision = TurnController().engage(organization=run.organization, lead=lead)
        lead.refresh_from_db()
        with transaction.atomic():
            applied = _persist_engagement_answers(lead, decision, source.pk)
            actions = list(decision.crm_actions or [])
            supported = [item for item in actions if item.get("type") in {"attribute_updates", "pipeline_transition", "add_note"}]
            blocked = [item.get("type", "unknown") for item in actions if item not in supported]
            receipts = CRMActionExecutor().execute(organization=run.organization, lead=lead,
                actions=supported, source_message=source) if supported else []
            if decision.should_engage and decision.message:
                # Store generated copy for subsequent reasoning/summary; never label it sent.
                outbound = WhatsAppMessage(organization=run.organization,
                    account=account, lead=lead, direction="outbound", external_id=f"operations-test:{turn.pk}:out",
                    body=decision.message, from_number="", to_number="", status="failed",
                    error="Isolated test: transport intentionally disabled",
                    raw_payload={"operations_test_run_id": str(run.pk), "shvya_ai": {
                        "source_inbound_message_id": str(source.pk), "next_requirement_id": decision.next_requirement_id,
                        "reason": decision.reason_code or decision.reason}})
                WhatsAppMessage.objects.bulk_create([outbound])
                # Reuse only the canonical question-ledger hook. Bulk persistence
                # intentionally avoids every scheduling/transport signal.
                from apps.ai_engagement.background_signals import remember_ai_qualification_question
                remember_ai_qualification_question(WhatsAppMessage, outbound, created=True)
        # CRM and question-ledger services lock their own Lead instances.
        # Summary must see the actual committed stage/attribute state.
        lead.refresh_from_db()
        summary_error = ""
        try:
            InternalSummaryService().generate_and_publish(organization=run.organization, lead=lead)
        except Exception as exc:
            # Keep real already-billed usage and state. Never replay a partial turn.
            summary_error = exc.__class__.__name__
        return {"reply": decision.message if decision.should_engage else "", "should_engage": decision.should_engage,
            "model": decision.model, "source_message_id": str(source.pk), "answer_persistence_applied": applied,
            "crm_results": receipts, "blocked_action_types": blocked, "selected_file_id": decision.file_document_id,
            "summary_status": "failed" if summary_error else "persisted", "summary_error_code": summary_error,
            "state": _lead_state(run), "transport_executed": False}


def run_ai_flow_test_turn(*, identity, arguments):
    arguments = arguments or {}
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization,
        capability=CAP_AI_FLOW_TEST_WRITE, tool_name="run_ai_flow_test_turn", arguments=arguments)
    key = _key(arguments)
    message = str(arguments.get("message") or "").strip()
    if not message or len(message) > 4000:
        raise OperationsToolError("message must be 1 to 4000 characters.")
    _reject_secret_like_content(message, field="message")
    digest = _digest({"message": message})
    run = _run(organization, arguments)
    existing = run.turns.filter(idempotency_key=key).first()
    if existing:
        if existing.input_digest != digest:
            raise OperationsToolError("idempotency_key was already used with different turn input.")
        return _execution({"turn_id": str(existing.pk), "status": existing.status, "result": existing.result,
            "idempotent_replay": True, "budget": budget_usage(run)}, run, reason)
    proposal = {"run_id": str(run.pk), "input_digest": digest, "idempotency_key": key,
        "configuration_digest": run.configuration_digest, "max_credits": run.max_credits, "max_provider_calls": run.max_provider_calls}
    if dry_run:
        return _execution({"status": "DRY_RUN", "proposal": proposal, "billable": True,
            "budget": budget_usage(run), "approval_required": True}, run, reason, dry_run=True, proposal=proposal)
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    with transaction.atomic():
        run = _run(organization, arguments, lock=True)
        existing = run.turns.filter(idempotency_key=key).first()
        if existing:
            if existing.input_digest != digest:
                raise OperationsToolError("idempotency_key was already used with different turn input.")
            return _execution({"turn_id": str(existing.pk), "status": existing.status, "result": existing.result, "idempotent_replay": True}, run, reason)
        if run.status != "ready" or run.cleaned_at:
            raise OperationsToolError("Test run is closed or cleaned up.")
        if run.active_turn_id:
            raise OperationsToolError("This run already has a running turn; inspect it before continuing.")
        if run.turns.count() >= run.max_turns:
            raise OperationsToolError("Test turn budget exhausted.")
        usage = budget_usage(run)
        if usage["remaining_credits"] <= 0 or run.provider_calls >= run.max_provider_calls:
            raise OperationsToolError("Test provider-call or credit budget exhausted.")
        if _configuration(organization) != run.configuration_digest:
            raise OperationsToolError("AI configuration changed since this test began. Create a fresh run.")
        turn = OperationsAIFlowTurn.objects.create(run=run, idempotency_key=key, input_digest=digest, message=message)
        run.active_turn_id = turn.pk
        run.save(update_fields=["active_turn_id", "updated_at"])
    result = {}
    status = "completed"
    try:
        result = _execute_production_turn(run, turn)
        if result.get("summary_status") != "persisted" or result.get("blocked_action_types"):
            status = "partial"
    except Exception as exc:
        status = "failed"
        result = {"error_code": exc.__class__.__name__, "state": _lead_state(run),
            "note": "A failed turn may contain already-persisted CRM state and billed calls. Reusing this key never runs it again."}
    # Process interruption (BaseException) deliberately leaves the committed
    # running claim in place. It must never be automatically billed/replayed.
    with transaction.atomic():
        locked = OperationsAIFlowRun.objects.select_for_update().get(pk=run.pk, organization=organization)
        turn.status, turn.result, turn.completed_at = status, json.loads(json.dumps(result, default=str)), timezone.now()
        turn.save(update_fields=["status", "result", "completed_at"])
        if locked.active_turn_id == turn.pk:
            locked.active_turn_id = None
            locked.save(update_fields=["active_turn_id", "updated_at"])
        run = locked
    return _execution({"turn_id": str(turn.pk), "status": status, "result": turn.result, "budget": budget_usage(run)}, run, reason)


def cleanup_ai_flow_test_run(*, identity, arguments):
    arguments = arguments or {}
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization,
        capability=CAP_AI_FLOW_TEST_WRITE, tool_name="cleanup_ai_flow_test_run", arguments=arguments)
    if "lead_ids" in arguments or "account_ids" in arguments:
        raise OperationsToolError("Cleanup accepts only run_id; fixture identifiers are server-owned.")
    run = _run(organization, arguments)
    proposal = {"run_id": str(run.pk), "lead_id": str(run.fixture_lead_id), "account_id": str(run.fixture_account_id)}
    if dry_run:
        return _execution({"status": "DRY_RUN", "proposal": proposal, "deletes_only_owned_test_fixtures": True, "approval_required": True}, run, reason, dry_run=True, proposal=proposal)
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    with transaction.atomic():
        run = _run(organization, arguments, lock=True)
        if run.active_turn_id:
            raise OperationsToolError("Cannot clean up a run while its turn is running.")
        if run.cleaned_at:
            return _execution({**_snapshot(run), "idempotent_replay": True}, run, reason)
        with owned_test_scope(run):
            lead = Lead.objects.filter(pk=run.fixture_lead_id, organization=organization, is_operations_test=True).first()
            account = WhatsAppAccount.objects.filter(pk=run.fixture_account_id, organization=organization, is_operations_test=True).first()
            if lead:
                lead.delete()
            if account:
                account.delete()
        run.status, run.cleaned_at = "cleaned", timezone.now()
        run.save(update_fields=["status", "cleaned_at", "updated_at"])
    return _execution({**_snapshot(run), "audit_and_results_retained": True}, run, reason)
