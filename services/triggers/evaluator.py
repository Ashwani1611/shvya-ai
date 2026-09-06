"""Durable, ordered event evaluation. Mutations serialize on the lead row."""

from contextvars import ContextVar
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.crm.models import Lead
from apps.triggers.models import SmartTrigger, TriggerEvent, TriggerRun

causal_rules = ContextVar("smart_trigger_causal_rules", default=())


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
                lead=lead, direction="inbound", created_at__gte=latest.created_at
            ).exists()
        )
    return True


def matches(rule, lead, payload):
    c = rule.conditions
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
    return True


def emit(lead, kind, key, payload=None):
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
                        trigger_type=kind,
                    ).values_list("id", flat=True)
                ],
                "snapshot": {
                    "pipeline": str(lead.pipeline_id),
                    "stage": str(lead.stage_id),
                    "attributes": lead.attributes or {},
                },
                **(payload or {}),
            },
        },
    )[0]


@transaction.atomic
def evaluate(event_id):
    # Same lock ordering as CRM actions: lead, then event/run.
    initial = TriggerEvent.objects.get(id=event_id)
    lead = (
        Lead.objects.select_for_update(of=("self",))
        .select_related("organization", "pipeline", "stage")
        .get(id=initial.lead_id, organization_id=initial.organization_id)
    )
    event = TriggerEvent.objects.select_for_update().get(id=event_id)
    if event.processed_at:
        return
    rules = SmartTrigger.objects.filter(
        organization_id=event.organization_id,
        organization__is_active=True,
        enabled=True,
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


def scan_timers():
    """Durable timer identities fire once per stage entry or unanswered outbound."""
    from apps.channels.models import WhatsAppMessage

    for rule in SmartTrigger.objects.filter(
        enabled=True, trigger_type__in=["stage_idle", "no_response"]
    ).iterator():
        cutoff = timezone.now() - delta(rule.conditions)
        leads = Lead.objects.filter(organization_id=rule.organization_id)
        if rule.trigger_type == "stage_idle":
            leads = leads.filter(stage_entered_at__lte=cutoff)
        for lead in leads.iterator():
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
