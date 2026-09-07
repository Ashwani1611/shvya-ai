"""Sync trustworthy WhatsApp contact names into Hosted CRM leads.

The hosted gateway resolves a direct chat's WhatsApp contact metadata and sends
``contactName``/``chatName`` plus a canonical peer phone. This module keeps the
CRM lead name useful without overwriting a deliberately curated lead name.
"""

from __future__ import annotations

from apps.channels.models import WhatsAppAccount
from apps.crm.models import Lead
from services.channels.hosted_whatsapp_service import normalize_whatsapp_number


_GENERIC_NAMES = {
    "lead",
    "new lead",
    "unknown",
    "unknown lead",
    "whatsapp lead",
    "whatsapp user",
}


def _clean(value):
    return str(value or "").strip()


def _phone(value):
    raw = _clean(value)
    if not raw or raw.endswith(("@lid", "@g.us")):
        return ""
    if raw.endswith("@c.us"):
        raw = raw.split("@", 1)[0]
    elif "@" in raw:
        return ""
    return normalize_whatsapp_number(phone_number=raw)


def _contact_name(item, phone):
    name = _clean(item.get("contactName") or item.get("chatName"))
    if not name:
        return ""
    if name.endswith(("@lid", "@c.us", "@g.us")):
        return ""
    if name.lower() in _GENERIC_NAMES:
        return ""

    name_digits = "".join(ch for ch in name if ch.isdigit())
    phone_digits = "".join(ch for ch in phone if ch.isdigit())
    if name.lstrip("+").isdigit() or (
        name_digits and phone_digits and name_digits == phone_digits
    ):
        return ""
    return name[:150]


def _peer_phone(item):
    phone = _phone(item.get("contactPhoneNumber") or item.get("peerPhone"))
    if phone:
        return phone

    candidate = item.get("to") if bool(item.get("fromMe")) else item.get("from")
    return _phone(candidate)


def _should_replace(lead, phone):
    current = _clean(lead.name)
    if not current:
        return True
    if current.lower() in _GENERIC_NAMES:
        return True

    current_digits = "".join(ch for ch in current if ch.isdigit())
    phone_digits = "".join(ch for ch in phone if ch.isdigit())
    if current.lstrip("+").isdigit() or (
        current_digits and phone_digits and current_digits == phone_digits
    ):
        return True

    # Leads created from WhatsApp are expected to follow the contact's current
    # WhatsApp display name. Manually curated/imported lead names are preserved.
    return lead.lead_source == "whatsapp_api"


def sync_hosted_contact_names(*, payload):
    """Apply WhatsApp contact names from one live/history gateway callback.

    Returns the number of leads whose names changed. Group chats and callbacks
    without a trustworthy direct-phone identity are ignored.
    """
    if not isinstance(payload, dict):
        return 0

    session_id = _clean(payload.get("sessionId"))
    event = _clean(payload.get("event")).lower()
    if not session_id or event not in {"message", "history_sync"}:
        return 0

    account = (
        WhatsAppAccount.objects.select_related("organization")
        .filter(
            id=session_id,
            connection_type="hosted",
            is_active=True,
        )
        .first()
    )
    if not account:
        return 0

    if event == "history_sync":
        items = [
            item
            for item in (payload.get("messages") or [])
            if isinstance(item, dict)
        ]
    else:
        items = [payload]

    names_by_phone = {}
    for item in items:
        if item.get("isGroup"):
            continue
        phone = _peer_phone(item)
        if not phone:
            continue
        name = _contact_name(item, phone)
        if name:
            names_by_phone[phone] = name

    if not names_by_phone:
        return 0

    changed = 0
    leads = Lead.objects.filter(
        organization=account.organization,
        phone__in=list(names_by_phone),
    )
    for lead in leads:
        name = names_by_phone.get(lead.phone)
        if not name or lead.name == name or not _should_replace(lead, lead.phone):
            continue
        lead.name = name
        lead.save(update_fields=["name", "updated_at"])
        changed += 1

    return changed
