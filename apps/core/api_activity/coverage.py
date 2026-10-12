"""Declared capture boundaries, separate from observed provider health."""

from django.db.models import Count, Max


INTEGRATIONS = (
    ("whatsapp_api", "WhatsApp API", "Provider requests and recorded message states", "Delivery depends on a recorded provider callback."),
    ("whatsapp_coexistence", "WhatsApp Coexistence", "Capability requests, echo, history and app-state handlers", "Ordinary sends share the WhatsApp API transport and appear under WhatsApp API."),
    ("whatsapp_hosted", "Hosted WhatsApp", "Gateway requests, saved session and message states", "Internal gateway events before a Django callback are outside this capture boundary."),
    ("instagram", "Instagram", "Provider requests and recorded message states", "Only receipt states actually exposed by the integration are available."),
    ("meta_templates", "Meta templates", "Template API requests and operation records", "Submission acceptance does not mean template approval."),
    ("meta_leads", "Meta Lead Ads", "Lead fetch and CRM processing outcomes", "HTTP acknowledgement does not prove a lead was saved."),
    ("indiamart", "IndiaMART", "Intake and recorded CRM outcomes", "Provider-side history and missing inquiries are not reconciled automatically."),
    ("justdial", "JustDial", "Intake and recorded CRM outcomes", "Provider-side history and missing inquiries are not reconciled automatically."),
    ("webhooks", "Outgoing webhooks", "Delivery attempts and stored outcomes", "A receiving server's response does not prove its downstream processing."),
    ("shvya_api", "SHVYA API", "Incoming HTTP request metadata", "Successful HTTP handling alone does not prove downstream completion."),
    ("celery", "Background jobs", "Publish, start, retry and execution outcome", "Only instrumented workers report lifecycle events; abrupt crashes can leave open work."),
    ("ai", "AI usage", "Settled AI-credit usage and correlated diagnostics", "Amounts are AI credits, not currency; unreported provider usage is not reconstructed."),
)

INTEGRATION_LABELS = {key: label for key, label, _, _ in INTEGRATIONS}


def coverage_snapshot(queryset):
    """Scope observations to the same time/org filters as the explorer."""
    observed = {
        item["integration"]: item
        for item in queryset.values("integration").annotate(
            count=Count("pk"), last_seen=Max("created_at")
        ).order_by()
    }
    rows = []
    for key, label, capture, limit in INTEGRATIONS:
        if key == "celery":
            item = queryset.filter(kind="task").aggregate(count=Count("pk"), last_seen=Max("created_at"))
            observed.pop(key, None)
        else:
            item = observed.pop(key, {})
        rows.append({
            "integration": key, "label": label, "capture": capture,
            "limit": limit, "count": item.get("count", 0),
            "last_seen": item.get("last_seen"),
        })
    for key, item in sorted(observed.items()):
        rows.append({
            "integration": key, "label": key.replace("_", " ").title(),
            "capture": "Recorded activity metadata", "limit": "Review individual records for the confirmed capture boundary.",
            "count": item["count"], "last_seen": item["last_seen"],
        })
    return rows
