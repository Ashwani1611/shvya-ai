"""Durable, ordered event evaluation. Mutations serialize on the lead row."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from apps.crm.models import Lead
from apps.triggers.models import SmartTrigger, TriggerEvent, TriggerRun

logger = logging.getLogger(__name__)

causal_rules = ContextVar("smart_trigger_causal_rules", default=())
_workflow_events_suppressed = ContextVar("workflow_events_suppressed", default=False)


@contextmanager
def suppress_workflow_events():
    """Suppress new workflow fan-out within one explicit provisioning context.

    ContextVars isolate concurrent requests and nested calls. This does not
    disable existing rules or cancel already-queued work for other leads.
    """
    token = _workflow_events_suppressed.set(True)
    try:
        yield
    finally:
        _workflow_events_suppressed.reset(token)


def delta(config):
    return timedelta(**{config["unit"]: config["duration"]})


def timer_valid(event, lead):
    if event.kind == "stage_idle":
        return event.payload.get("entry") == lead.stage_entered_at.isoformat()
    if event.kind == "no_response":
        from apps.channels.models import WhatsAppMessage

        latest = (
            TriggerEvent.objects.filter(
                lead=lead, organization_id=lead.organization_id, kind="outbound_sent"
            )
            .order_by("-created_at")
            .first()
        )
        return bool(
            latest
            and str(latest.id) == event.payload.get("sent_event")
            and not WhatsAppMessage.objects.filter(
                lead=lead, organization_id=lead.organization_id,
                direction="inbound", created_at__gte=latest.created_at
            ).exists()
        )
    return True


def matches(rule, lead, payload):
    c = rule.conditions
    if c.get("sources") and getattr(lead, "lead_source", None) not in c["sources"]:
        return False
    scopes = c.get("scopes", [])
    if scopes and not any(
        str(lead.pipeline_id) == s["pipeline"] and str(lead.stage_id) in s["stages"]
        for s in scopes
    ):
        return False
    if not scopes and rule.trigger_type != "sequence_ended":
        return False
    for condition in c.get("attributes", []):
        raw = (lead.attributes or {}).get(condition["key"])
        if raw is None:
            return False
        actual = str(raw).casefold()
        if not any(
            actual == value if condition["match"] == "equals" else value in actual
            for value in condition["values"]
        ):
            return False
    if rule.trigger_type == "sequence_ended":
        return payload.get("sequence") in c.get("sequences", [])
    if rule.trigger_type == "keyword":
        text = payload.get("body", "").casefold()
        return any(k == "*" or k in text for k in c["keywords"])
    if rule.trigger_type == "call_logged":
        return (
            payload.get("status") == c["call_status"] and payload.get("manual") is True
        )
    if rule.trigger_type == "call_intelligence_ready":
        intent = payload.get("intent", "unknown")
        try:
            score = int(payload.get("ai_score") or 0)
        except (TypeError, ValueError):
            score = 0
        return (
            c.get("intent", "any") in {"any", intent}
            and score >= int(c.get("min_ai_score", 0))
        )
    return True


def emit(lead, kind, key, payload=None):
    if _workflow_events_suppressed.get() or getattr(lead, "is_operations_test", False):
        return None
    return TriggerEvent.objects.get_or_create(
        key=key,
        defaults={
            "organization_id": lead.organization_id,
            "lead": lead,
            "kind": kind,
            "payload": {
                "causal_rules": list(causal_rules.get()),
                "eligible_rules": [
                    str(pk)
                    for pk in SmartTrigger.objects.filter(
                        organization_id=lead.organization_id,
                        enabled=True,
                        is_active=True,
                        trigger_type=kind,
                    ).values_list("id", flat=True)
                ],
                "snapshot": {
                    "pipeline": str(lead.pipeline_id),
                    "stage": str(lead.stage_id),
                    "attributes": lead.attributes or {},
                    "lead_source": lead.lead_source,
                },
                **(payload or {}),
            },
        },
    )[0]


@transaction.atomic
def evaluate(event_id):
    # Same lock ordering as CRM actions: lead, then event/run.
    initial = TriggerEvent.objects.get(id=event_id)
    if TriggerEvent.objects.filter(pk=initial.pk, lead__is_operations_test=True).exists():
        TriggerEvent.objects.filter(pk=initial.pk, processed_at__isnull=True).update(processed_at=timezone.now())
        return
    lead = (
        Lead.objects.select_for_update(of=("self",))
        .select_related("organization", "pipeline", "stage")
        .get(id=initial.lead_id, organization_id=initial.organization_id)
    )
    event = TriggerEvent.objects.select_for_update().get(id=event_id)
    if event.processed_at:
        return
    if getattr(lead, "is_operations_test", False):
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at"])
        return
    rules = SmartTrigger.objects.filter(
        organization_id=event.organization_id,
        organization__is_active=True,
        enabled=True,
        is_active=True,
        trigger_type=event.kind,
        created_at__lte=event.created_at,
        id__in=event.payload.get("eligible_rules", []),
    ).exclude(id__in=event.payload.get("causal_rules", []))
    if event.payload.get("rule"):
        rules = rules.filter(id=event.payload["rule"])
    for rule in rules:
        from types import SimpleNamespace

        snapshot = event.payload.get("snapshot")
        matching_lead = (
            SimpleNamespace(
                pipeline_id=snapshot["pipeline"],
                stage_id=snapshot["stage"],
                attributes=snapshot["attributes"],
                # Read the authoritative creation origin after any existing
                # inbound-source normalization in the committing transaction.
                # Never substitute a custom SOURCE attribute or channel guess.
                lead_source=lead.lead_source,
            )
            if snapshot and event.kind not in ("stage_idle", "no_response")
            else lead
        )
        if not matches(rule, matching_lead, event.payload):
            continue
        if not timer_valid(event, lead):
            continue
        # Snapshot all matches before actions run. Later changes cannot rewrite this event.
        cooling = (
            TriggerRun.objects.filter(
                rule=rule,
                lead=lead,
                created_at__gte=timezone.now() - timedelta(seconds=30),
            )
            .exclude(status__in=["skipped", "failed"])
            .exists()
        )
        TriggerRun.objects.get_or_create(
            rule=rule,
            event=event,
            defaults={
                "lead": lead,
                "action_type": rule.action_type,
                "action": rule.action,
                "due_at": timezone.now(),
                "status": "skipped" if cooling else "pending",
                "detail": "Rule cooldown (30 seconds)." if cooling else "",
            },
        )
    event.processed_at = timezone.now()
    event.save(update_fields=["processed_at"])


def _timer_rule_batch():
    """Return a bounded, tenant-fair set of timer rules for one Beat pass."""
    per_organization = max(
        1,
        int(getattr(settings, "WORKFLOW_TIMER_RULES_PER_ORGANIZATION", 2)),
    )
    limit = max(
        1,
        int(getattr(settings, "WORKFLOW_TIMER_RULES_PER_PASS", 40)),
    )
    rules = (
        SmartTrigger.objects.filter(
            enabled=True,
            is_active=True,
            organization__is_active=True,
            trigger_type__in=["stage_idle", "no_response"],
        )
        .annotate(
            _tenant_rank=Window(
                expression=RowNumber(),
                partition_by=[F("organization_id")],
                order_by=[
                    F("timer_scan_at").asc(nulls_first=True),
                    F("id").asc(),
                ],
            )
        )
        .filter(_tenant_rank__lte=per_organization)
        .order_by(F("timer_scan_at").asc(nulls_first=True), "id")
    )
    return list(rules[:limit])


def _timer_lead_batch(rule, leads):
    """Advance a durable per-rule lead cursor without an unbounded scan."""
    limit = max(
        1,
        int(getattr(settings, "WORKFLOW_TIMER_LEADS_PER_RULE", 100)),
    )
    queryset = leads.order_by("id")
    if rule.timer_lead_cursor:
        queryset = queryset.filter(id__gt=rule.timer_lead_cursor)

    batch = list(queryset[:limit])
    if batch:
        cursor = batch[-1].id
        SmartTrigger.objects.filter(pk=rule.pk).update(timer_lead_cursor=cursor)
        rule.timer_lead_cursor = cursor
        return batch

    # Reaching the end consumes one empty pass and resets the cursor. The next
    # pass starts from the beginning, so new/changed leads are eventually
    # revisited while each invocation remains strictly bounded.
    if rule.timer_lead_cursor:
        SmartTrigger.objects.filter(pk=rule.pk).update(timer_lead_cursor=None)
        rule.timer_lead_cursor = None
    return []


def scan_timers():
    """Scan timers incrementally so one tenant cannot monopolize automation."""
    for rule in _timer_rule_batch():
        try:
            _scan_timer(rule)
        except Exception:
            logger.exception("Workflow timer scan failed: %s", rule.id)
        finally:
            scanned_at = timezone.now()
            SmartTrigger.objects.filter(pk=rule.pk).update(timer_scan_at=scanned_at)
            rule.timer_scan_at = scanned_at


def _scan_timer(rule):
    """Durable identities fire once per stage entry or unanswered outbound."""
    from apps.channels.models import WhatsAppMessage

    cutoff = timezone.now() - delta(rule.conditions)
    leads = Lead.objects.filter(organization_id=rule.organization_id)

    # Pipeline/stage scope is cheap to apply in PostgreSQL and drastically
    # reduces cursor-cycle time for organizations with large CRM datasets.
    scopes = list(rule.conditions.get("scopes") or [])
    if scopes:
        scope_query = Q(pk__in=[])
        for scope in scopes:
            pipeline_id = scope.get("pipeline")
            stage_ids = list(scope.get("stages") or [])
            if pipeline_id and stage_ids:
                scope_query |= Q(
                    pipeline_id=pipeline_id,
                    stage_id__in=stage_ids,
                )
        leads = leads.filter(scope_query)

    if rule.trigger_type == "stage_idle":
        leads = leads.filter(stage_entered_at__lte=cutoff)

    for lead in _timer_lead_batch(rule, leads):
        if not matches(rule, lead, {}):
            continue
        if rule.trigger_type == "stage_idle":
            entry = lead.stage_entered_at.isoformat()
            emit(
                lead,
                "stage_idle",
                f"idle:{rule.id}:{lead.id}:{entry}",
                {"rule": str(rule.id), "entry": entry},
            )
        else:
            message = (
                TriggerEvent.objects.filter(
                    lead=lead,
                    organization_id=lead.organization_id,
                    kind="outbound_sent",
                )
                .order_by("-created_at")
                .first()
            )
            if (
                message
                and message.created_at <= cutoff
                and not WhatsAppMessage.objects.filter(
                    lead=lead,
                    organization_id=lead.organization_id,
                    direction="inbound",
                    created_at__gte=message.created_at,
                ).exists()
            ):
                emit(
                    lead,
                    "no_response",
                    f"no-response:{rule.id}:{message.id}",
                    {"rule": str(rule.id), "sent_event": str(message.id)},
                )
