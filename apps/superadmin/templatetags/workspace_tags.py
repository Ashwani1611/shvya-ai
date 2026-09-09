from django import template
from django.utils.dateparse import parse_datetime

from apps.channels.models import WhatsAppAccount
from apps.crm.models.pipeline import Pipeline

register = template.Library()


def _digits(value):
    return "".join(character for character in str(value or "") if character.isdigit())


def _pipeline_number_candidates(pipeline):
    phone = _digits(pipeline.phone_number)
    country_code = _digits(pipeline.country_code)
    candidates = set()

    if phone:
        candidates.add(phone)
    if country_code and phone and not phone.startswith(country_code):
        candidates.add(f"{country_code}{phone}")

    return candidates


def _account_matches_pipeline(account, pipeline):
    account_number = _digits(account.display_phone_number)
    if not account_number:
        return False

    candidates = _pipeline_number_candidates(pipeline)
    return account_number in candidates


def _channel_name(account):
    if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        return "Hosted"
    return "WhatsApp API"


def _build_pipeline_channel_items(organization, accounts):
    """Resolve one effective WhatsApp channel for every pipeline-linked number.

    A pipeline can only operate through one linked number at a time. Historical or
    stale WhatsAppAccount rows can leave both API and Hosted records active for the
    same number, so the Superadmin UI must not count those rows as two channels.
    The most recently updated connected account for the pipeline number is treated
    as the effective channel.
    """
    pipelines = list(
        Pipeline.objects.filter(organization=organization).order_by("name", "id")
    )
    active_accounts = [
        account
        for account in accounts
        if account.is_active and account.status == WhatsAppAccount.Status.CONNECTED
    ]
    items = []

    for pipeline in pipelines:
        matching_accounts = [
            account for account in accounts if _account_matches_pipeline(account, pipeline)
        ]
        active_matches = [
            account
            for account in matching_accounts
            if account.is_active and account.status == WhatsAppAccount.Status.CONNECTED
        ]

        # A small compatibility fallback for older records where Meta's display
        # phone number was not persisted. It is safe only when the organization has
        # exactly one pipeline and one connected WhatsApp account.
        if not matching_accounts and len(pipelines) == 1 and len(active_accounts) == 1:
            matching_accounts = [active_accounts[0]]
            active_matches = [active_accounts[0]]

        selected = active_matches[0] if active_matches else (
            matching_accounts[0] if matching_accounts else None
        )
        linked_phone = " ".join(
            part for part in [pipeline.country_code, pipeline.phone_number] if part
        ).strip()

        if selected is None:
            items.append(
                {
                    "pipeline": pipeline.name,
                    "phone": linked_phone,
                    "label": "Not connected",
                    "active": False,
                    "inactive_since": "",
                }
            )
            continue

        is_connected = (
            selected.is_active
            and selected.status == WhatsAppAccount.Status.CONNECTED
        )
        items.append(
            {
                "pipeline": pipeline.name,
                "phone": selected.display_phone_number or linked_phone,
                "label": _channel_name(selected),
                "active": is_connected,
                "inactive_since": (
                    "" if is_connected else selected.updated_at.isoformat()
                ),
            }
        )

    return items


@register.simple_tag
def organization_workspace_state(organization):
    """Return compact UI state for an organization's Superadmin workspace."""
    accounts = list(
        WhatsAppAccount.objects.filter(organization=organization).order_by("-updated_at")
    )
    channel_items = _build_pipeline_channel_items(organization, accounts)
    active_items = [item for item in channel_items if item["active"]]

    if active_items:
        if len(active_items) == 1:
            item = active_items[0]
            channel_label = f"{item['label']} active"
            channel_detail = " · ".join(
                value for value in [item["pipeline"], item["phone"]] if value
            )
        else:
            channel_label = f"{len(active_items)} pipelines connected"
            channel_detail = "Each pipeline uses one linked WhatsApp channel"
        channel_active = True
        inactive_since = None
    elif channel_items:
        channel_label = "Inactive"
        channel_detail = "No pipeline-linked WhatsApp channel is currently connected"
        channel_active = False
        inactive_since = accounts[0].updated_at if accounts else None
    else:
        active_accounts = [
            account
            for account in accounts
            if account.is_active and account.status == WhatsAppAccount.Status.CONNECTED
        ]
        selected = active_accounts[0] if active_accounts else (accounts[0] if accounts else None)

        if selected and selected in active_accounts:
            channel_label = f"{_channel_name(selected)} active"
            channel_detail = selected.display_phone_number or "Organization WhatsApp channel"
            channel_active = True
            inactive_since = None
        elif selected:
            channel_label = "Inactive"
            channel_detail = "No Hosted or WhatsApp API number is currently connected"
            channel_active = False
            inactive_since = selected.updated_at
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
        "channel_items": channel_items,
        "inactive_since": inactive_since,
        "active_count": len(active_items),
        "total_count": len(channel_items) if channel_items else len(accounts),
        "note_updated_at": note_updated_at,
    }
