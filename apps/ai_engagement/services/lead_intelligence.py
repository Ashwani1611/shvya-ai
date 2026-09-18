"""Accepted-turn persistence for Phase 6/8/9; all observations are tenant scoped."""
from __future__ import annotations

import logging
from django.db import transaction
from django.db.models import Count

from apps.ai_engagement.services.customer_memory import explicit_facts, reported_events
from apps.ai_engagement.services.sales_intelligence import (
    ObjectionEngine, detect_signals, score_signals, settings_section,
)
from apps.ai_engagement.services.tenant_guard import TenantGuard, TenantScopeError

logger = logging.getLogger(__name__)


def analyze_turn(*, organization, lead, source_message, intent_decision=None):
    from apps.ai_engagement.services.organization_runtime_profile import get_organization_ai_runtime_profile

    guard = TenantGuard(organization)
    guard.validate_current_lead_context(lead)
    guard.validate_message(source_message, lead=lead)
    if source_message.direction != "inbound":
        raise TenantScopeError(object_type="inbound_message")
    settings = organization.settings or {}
    profile = get_organization_ai_runtime_profile(organization=organization, lead=lead).as_dict()
    definitions = (profile.get("crm_capabilities") or {}).get("attributes") or []
    facts = explicit_facts(text=source_message.body, source_message_id=source_message.pk,
                           settings=settings, definitions=definitions,
                           language=getattr(intent_decision, "language", None))
    objections = ObjectionEngine().detect(text=source_message.body, settings=settings,
                                          intent_decision=intent_decision)
    return {"facts": facts, "objections": objections,
            "reported_events": reported_events(source_message.body)}


def signal_summary(*, organization, lead):
    from apps.ai_engagement.models import LeadSignal
    TenantGuard(organization).validate_lead(lead)
    rows = LeadSignal.objects.filter(organization=organization, lead=lead).values("kind").annotate(count=Count("id"))
    return score_signals(counts={row["kind"]: row["count"] for row in rows}, settings=organization.settings)


def observe_accepted_turn(*, lead, source_message_id):
    """Called only after the existing final validation accepts an inbound turn.

    Use a savepoint so optional enrichment failure cannot poison the enclosing
    reply transaction. Tenant failures remain fatal. Signals have database
    uniqueness, and memory remains on the existing organization-owned Lead.
    """
    from apps.ai_engagement.models import LeadSignal
    from apps.ai_engagement.services import conversation_policy_runtime as policy
    from apps.ai_engagement.services.structured_memory import StructuredLeadMemoryService
    from apps.ai_engagement.services.trace_service import record, mark_error
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead

    organization = lead.organization
    guard = TenantGuard(organization)
    guard.validate_current_lead_context(lead)
    source = WhatsAppMessage.objects.select_related("account", "lead").get(
        pk=source_message_id, organization=organization, lead=lead, direction="inbound")
    guard.validate_message(source, lead=lead)
    turn = policy._TURN.get()
    intent = None
    if (isinstance(turn, dict) and str(turn.get("organization_id")) == str(organization.pk)
            and str(turn.get("lead_id")) == str(lead.pk)
            and str(turn.get("source_message_id")) == str(source.pk)):
        intent = turn.get("intent_decision")
    try:
        with transaction.atomic():
            locked = Lead.objects.select_for_update().select_related(
                "organization", "pipeline", "stage").get(pk=lead.pk, organization=organization)
            data = analyze_turn(organization=organization, lead=locked,
                                source_message=source, intent_decision=intent)
            memory_cfg = settings_section(organization.settings, "ai_memory")
            objection_cfg = settings_section(organization.settings, "ai_objections")
            persist_objections = objection_cfg.get("persist_memory", False) is True
            service = StructuredLeadMemoryService()
            if memory_cfg.get("enabled", True) is True:
                if data["facts"]:
                    snapshot, mutations = service.merge_facts(
                        organization=organization, lead=locked, facts=data["facts"],
                        source_message_id=str(source.pk), source_type="explicit_customer")
                    record("memory", service.trace_payload(snapshot=snapshot, mutations=mutations))
                events = list(data["reported_events"])
                if persist_objections:
                    events += [{"kind": "objection:" + item.category, "text": item.evidence}
                               for item in data["objections"]]
                if events:
                    snapshot = service.merge_events(organization=organization, lead=locked,
                                                    source_message=source, events=events)
                    record("memory", {"event_count": len(snapshot["events"])})
            if settings_section(organization.settings, "ai_signals").get("enabled", True) is True:
                prior = LeadSignal.objects.filter(organization=organization, lead=locked).exclude(
                    source_message_id=source.pk).values("source_message_id").distinct()[:2]
                signals = detect_signals(
                    text=source.body, intent_decision=intent, facts=data["facts"],
                    objections=data["objections"], repeated=len(prior) >= 2)
                signals = [("engaged", ""), *signals]
                LeadSignal.objects.bulk_create([
                    LeadSignal(organization=organization, lead=locked, source_message_id=source.pk,
                               kind=kind, detail=detail) for kind, detail in signals
                ], ignore_conflicts=True)
                record("signals", {"observed": [kind for kind, _ in signals],
                                   **signal_summary(organization=organization, lead=locked)})
            record("objections", {"categories": [item.category for item in data["objections"]],
                                  "persisted_to_memory": persist_objections and memory_cfg.get("enabled", True) is True})
            # Refresh from the locked object so the caller cannot later overwrite
            # accepted memory when finalizing the same lead.
            lead.attributes = locked.attributes
    except TenantScopeError:
        raise
    except Exception as exc:
        logger.exception("Accepted-turn intelligence unavailable")
        mark_error(step="lead_intelligence", exc=exc, code="LEAD_INTELLIGENCE_FAILED")


def record_explicit_opt_out(*, lead, source_message):
    """Observe a deterministic inbound opt-out even when it disables AI before
    the accepted-reply hook. This neither sends nor changes qualification/state.
    """
    from apps.ai_engagement.models import LeadSignal
    from apps.ai_engagement.services.runtime_state import is_explicit_opt_out
    organization = lead.organization
    if (source_message.direction != "inbound" or not is_explicit_opt_out(source_message.body)
            or settings_section(organization.settings, "ai_signals").get("enabled", True) is not True):
        return
    TenantGuard(organization).validate_message(source_message, lead=lead)
    try:
        with transaction.atomic():
            LeadSignal.objects.get_or_create(organization=organization, lead=lead,
                source_message_id=source_message.pk, kind="opt_out", detail="")
    except Exception:
        logger.exception("Optional opt-out signal observation failed")
