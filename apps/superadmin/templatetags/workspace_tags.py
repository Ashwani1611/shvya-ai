from django import template
from django.utils.dateparse import parse_datetime

from apps.channels.models import WhatsAppAccount

register = template.Library()


@register.simple_tag
def organization_workspace_state(organization):
    """Return compact UI state for an organization's Superadmin workspace."""
    accounts = list(
        WhatsAppAccount.objects.filter(organization=organization).order_by("-updated_at")
    )

    active_accounts = [
        account
        for account in accounts
        if account.is_active and account.status == WhatsAppAccount.Status.CONNECTED
    ]
    api_active = any(
        account.connection_type == WhatsAppAccount.ConnectionType.API
        for account in active_accounts
    )
    hosted_active = any(
        account.connection_type == WhatsAppAccount.ConnectionType.coexisted
        for account in active_accounts
    )

    if active_accounts:
        if api_active and hosted_active:
            channel_label = "API + Hosted active"
            channel_detail = f"{len(active_accounts)} connected numbers"
        elif hosted_active:
            channel_label = "Hosted active"
            channel_detail = f"{len(active_accounts)} connected hosted number{'s' if len(active_accounts) != 1 else ''}"
        else:
            channel_label = "WhatsApp API active"
            channel_detail = f"{len(active_accounts)} connected API number{'s' if len(active_accounts) != 1 else ''}"
        channel_active = True
        inactive_since = None
    elif accounts:
        channel_label = "Inactive"
        channel_detail = "No Hosted or WhatsApp API number is currently connected"
        channel_active = False
        inactive_since = accounts[0].updated_at
    else:
        channel_label = "Never connected"
        channel_detail = "No Hosted or WhatsApp API account has been connected yet"
        channel_active = False
        inactive_since = None

    note_updated_at = None
    raw_note_updated_at = (organization.settings or {}).get(
        "operational_notes_updated_at"
    )
    if raw_note_updated_at:
        note_updated_at = parse_datetime(str(raw_note_updated_at))

    return {
        "channel_active": channel_active,
        "channel_label": channel_label,
        "channel_detail": channel_detail,
        "inactive_since": inactive_since,
        "active_count": len(active_accounts),
        "total_count": len(accounts),
        "note_updated_at": note_updated_at,
    }
