"""Superadmin investigation controls; never dispatch provider actions."""

from collections import Counter
from datetime import timedelta
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.urls import reverse

from apps.core.models import APIActivity, APIIncident, APIIncidentEvent

from .queries import ISSUE_STATUSES, OPEN_STATUSES, serialize_activity
from .sanitization import sanitize_metadata, sanitize_text


class InvestigationError(ValueError):
    pass


class StaleInvestigation(InvestigationError):
    pass


def needs_attention(activity):
    return activity.status in ISSUE_STATUSES or bool(
        activity.status in OPEN_STATUSES and activity.expected_by and activity.expected_by < timezone.now()
    )


def journey_report(activity):
    """Bound links, preserve attribution, and distinguish correlation from causality."""
    scope = APIActivity.objects.filter(organization_id=activity.organization_id)
    relation = Q(pk=activity.pk) | Q(correlation_id=activity.correlation_id)
    if activity.organization_id:
        if activity.entity_type and activity.entity_id:
            relation |= Q(entity_type=activity.entity_type, entity_id=activity.entity_id)
        if activity.provider_reference:
            relation |= Q(integration=activity.integration, provider_reference=activity.provider_reference)
    else:
        scope = scope.filter(organization_reference=activity.organization_reference)
    initial = list(scope.filter(relation).values_list("pk", "parent_id")[:101])
    ids = {item[0] for item in initial[:100]} | {activity.pk}
    parents = {item[1] for item in initial[:100] if item[1]}
    if activity.parent_id:
        parents.add(activity.parent_id)
    rows = list(scope.filter(Q(pk__in=ids | parents) | Q(parent_id__in=ids)).select_related(
        "organization"
    ).defer("metadata").order_by("created_at", "pk")[:101])
    truncated = len(initial) > 100 or len(rows) > 100
    # Keep the opened activity visible even when old associations fill the limit.
    rows = rows[:100]
    if activity.pk not in {row.pk for row in rows}:
        rows = [activity, *rows[:99]]
        rows.sort(key=lambda row: (row.created_at, str(row.pk)))
    present = {row.pk for row in rows}
    results = []
    counts = Counter()
    for row in rows:
        item = serialize_activity(row)
        item["is_anchor"] = row.pk == activity.pk
        item["link_basis"] = (
            "Opened activity" if item["is_anchor"] else
            "Recorded parent link" if row.parent_id in present or row.pk in parents else
            "Shared correlation" if row.correlation_id == activity.correlation_id else
            "Shared domain reference"
        )
        item["parent_url"] = None
        if row.parent_id in present:
            item["parent_url"] = reverse("superadmin-api-activity", kwargs={"activity_id": row.parent_id})
        results.append(item)
        counts[row.status] += 1
    return {"results": results, "counts": dict(counts), "truncated": truncated,
            "missing_parents": sum(bool(row.parent_id and row.parent_id not in present) for row in rows)}


def saved_outcome(activity):
    """Read only safe fields from the canonical tenant-scoped object, without HTTP."""
    unsupported = {"supported": False, "label": "No saved-outcome adapter",
                   "explanation": "Use the recorded timeline and the provider's supported history. This check does not contact a provider."}
    if not activity.organization_id:
        return {**unsupported, "label": "Organization attribution required"}
    try:
        entity_id = uuid.UUID(activity.entity_id)
        connection_id = uuid.UUID(activity.connection_id) if activity.connection_id else None
    except (TypeError, ValueError, AttributeError):
        return unsupported
    if activity.entity_type == "message" and activity.integration in {
        "whatsapp_api", "whatsapp_coexistence", "whatsapp_hosted", "instagram",
    }:
        if activity.integration == "instagram":
            from apps.channels.instagram_models import InstagramMessage as model
        else:
            from apps.channels.models import WhatsAppMessage as model
        rows = model.objects.filter(pk=entity_id, organization_id=activity.organization_id,
                                    account__organization_id=activity.organization_id)
        if activity.integration == "whatsapp_hosted":
            rows = rows.filter(account__connection_type="hosted")
        elif activity.integration != "instagram":
            rows = rows.exclude(account__connection_type="hosted")
        if connection_id:
            rows = rows.filter(account_id=connection_id)
        row = rows.values("status", "external_id", "updated_at").first()
        if row and activity.provider_reference and row["external_id"] != activity.provider_reference:
            return {**unsupported, "label": "Provider reference does not match saved object"}
        descriptions = {
            "queued": "Saved message is queued; this does not prove a worker will execute it.",
            "sent": "Saved send is accepted; delivery remains unconfirmed.",
            "delivered": "The canonical message records a delivered status.",
            "read": "The canonical message records a read status.",
            "received": "The canonical message records receipt of an inbound message.",
            "failed": "The canonical message records failure; inspect the error and routing evidence.",
        }
    elif activity.entity_type == "webhook" and activity.integration == "webhooks":
        from apps.integrations.models import WebhookDelivery
        rows = WebhookDelivery.objects.filter(pk=entity_id, organization_id=activity.organization_id,
                                              webhook__organization_id=activity.organization_id)
        if connection_id:
            rows = rows.filter(webhook_id=connection_id)
        row = rows.values("status", "updated_at").first()
        descriptions = {"sent": "Receiver acknowledgement is saved; downstream processing is not confirmed.",
                        "queued": "Saved webhook is queued.", "retrying": "Saved webhook is awaiting a retry.",
                        "failed": "Saved webhook delivery is failed."}
    else:
        return unsupported
    if row is None:
        return {**unsupported, "label": "No matching object in this organization and connection"}
    return {"supported": True, "label": "Saved domain state", "status": row["status"],
            "observed_at": row["updated_at"].isoformat(),
            "explanation": descriptions.get(row["status"], "Inspect the canonical object for its recorded state.")}


def serialize_incident(incident):
    now = timezone.now()
    return {"id": incident.pk, "activity_id": str(incident.activity_id), "status": incident.status,
            "severity": incident.severity, "owner_id": str(incident.owner_id or ""),
            "owner_label": incident.owner.email if incident.owner else "Unassigned",
            "due_at": incident.due_at, "snoozed_until": incident.snoozed_until,
            "resolved_at": incident.resolved_at, "version": incident.version,
            "is_snoozed": bool(incident.snoozed_until and incident.snoozed_until > now),
            "is_overdue": bool(incident.status != "resolved" and incident.due_at and incident.due_at < now
                               and not (incident.snoozed_until and incident.snoozed_until > now))}


@transaction.atomic
def update_incident(activity_id, actor, data):
    if not actor.is_authenticated or not actor.is_active or not actor.is_superuser:
        raise InvestigationError("Superadmin access required.")
    try:
        activity = APIActivity.objects.select_for_update().get(pk=activity_id)
    except APIActivity.DoesNotExist:
        raise InvestigationError("This evidence is no longer available. Reload the API explorer.") from None
    incident = APIIncident.objects.select_for_update().filter(activity=activity).first()
    action = data.get("action", "")
    if action not in {"open", "claim", "assign", "acknowledge", "resolve", "reopen", "snooze", "deadline", "severity", "note", "check"}:
        raise InvestigationError("Choose a supported investigation action.")
    try:
        version = int(data.get("version", ""))
    except (TypeError, ValueError):
        raise InvestigationError("Reload the investigation before saving.") from None
    if version != (incident.version if incident else 0):
        raise StaleInvestigation("Another operator changed this investigation. Reload before saving.")
    if incident is None:
        if not needs_attention(activity):
            raise InvestigationError("Open investigations from a record that needs attention.")
        incident = APIIncident.objects.create(activity=activity, due_at=timezone.now() + timedelta(hours=24))
    if action == "open" and incident.version:
        raise InvestigationError("This investigation is already open.")
    note = sanitize_text(str(data.get("note", "")), max_length=500).strip()
    if action in {"resolve", "reopen", "note"} and not note:
        raise InvestigationError("Add a short operational reason, without customer content or secrets.")
    metadata = {}
    if action == "claim":
        if incident.owner_id and incident.owner_id != actor.pk:
            raise InvestigationError("Another operator owns this investigation. Use explicit assignment to change its owner.")
        incident.owner = actor
    elif action == "assign":
        owner_id = data.get("owner", "")
        try:
            owner = get_user_model().objects.filter(pk=uuid.UUID(owner_id), is_active=True, is_superuser=True).first() if owner_id else None
        except (ValueError, TypeError):
            raise InvestigationError("Choose an active Superadmin owner.") from None
        if owner_id and owner is None:
            raise InvestigationError("Choose an active Superadmin owner.")
        incident.owner = owner
    elif action == "acknowledge":
        if incident.status == "resolved":
            raise InvestigationError("Reopen the investigation before acknowledging it.")
        incident.status = "acknowledged"
        incident.owner = incident.owner or actor
    elif action == "resolve":
        incident.status, incident.resolved_at = "resolved", timezone.now()
        incident.snoozed_until = None
    elif action == "reopen":
        incident.status, incident.resolved_at = "open", None
        incident.snoozed_until = None
    elif action in {"snooze", "deadline"}:
        if incident.status == "resolved":
            raise InvestigationError("Reopen the investigation before scheduling it.")
        try:
            hours = int(data.get("hours", ""))
        except (TypeError, ValueError):
            raise InvestigationError("Choose 1 to 168 hours.") from None
        if not 1 <= hours <= 168:
            raise InvestigationError("Choose 1 to 168 hours.")
        setattr(incident, "snoozed_until" if action == "snooze" else "due_at", timezone.now() + timedelta(hours=hours))
    elif action == "severity":
        if data.get("severity") not in {"low", "medium", "high"}:
            raise InvestigationError("Choose low, medium or high severity.")
        incident.severity = data["severity"]
    elif action == "check":
        metadata["saved_outcome"] = saved_outcome(activity)
    incident.version += 1
    incident.save()
    metadata.update(status=incident.status, severity=incident.severity, owner_id=str(incident.owner_id or ""),
                    version=incident.version)
    APIIncidentEvent.objects.create(incident=incident, actor=actor, action=action, note=note,
                                    metadata=sanitize_metadata(metadata))
    return incident
