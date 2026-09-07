"""Existing-chat ignore-list behavior for Hosted WhatsApp accounts."""

from dataclasses import dataclass

from django.db import transaction

from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount
from apps.channels.providers.whatsapp_web import (
    WhatsAppWebClient,
    WhatsAppWebGatewayError,
)


class HostedIgnoreSyncError(ValueError):
    """Raised when an existing-chat snapshot cannot be completed safely."""


@dataclass(frozen=True)
class HostedIgnoreSyncResult:
    account_count: int
    contact_count: int


def _normalize_phone(value):
    # Imported lazily to avoid a module-import cycle: the Hosted message service
    # imports the ignore-list lookup helpers below.
    from services.channels.hosted_whatsapp_service import normalize_whatsapp_number

    return normalize_whatsapp_number(phone_number=value)


def _connected_hosted_accounts(organization):
    return list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        ).order_by("connected_at")
    )


def _normalize_gateway_chat(*, account, item):
    if not isinstance(item, dict) or item.get("isGroup"):
        return None

    phone_number = _normalize_phone(item.get("phoneNumber"))
    if not phone_number:
        return None

    own_number = _normalize_phone(
        account.display_phone_number or account.phone_number_id
    )
    if own_number and phone_number == own_number:
        return None

    contact_name = str(item.get("contactName") or "").strip()[:180]
    chat_id = str(item.get("chatId") or "").strip()[:160]

    return HostedChatIgnoreContact(
        organization=account.organization,
        account=account,
        phone_number=phone_number,
        contact_name=contact_name,
        chat_id=chat_id,
    )


def is_hosted_contact_ignored(*, account, phone_number):
    normalized = _normalize_phone(phone_number)
    if not normalized:
        return False

    return HostedChatIgnoreContact.objects.filter(
        organization=account.organization,
        account=account,
        phone_number=normalized,
    ).exists()


def ignored_contact_for_chat(*, account, chat_id):
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        return None

    return HostedChatIgnoreContact.objects.filter(
        organization=account.organization,
        account=account,
        chat_id=chat_id,
    ).first()


def sync_existing_hosted_chats(*, organization):
    """Replace an organization's ignore list with a complete fresh snapshot.

    Every connected Hosted Account is read from the gateway before any database
    rows are deleted.  If one account fails or any LID chat cannot be resolved
    to a real phone number, the old ignore list remains untouched.  This avoids
    an incomplete snapshot accidentally allowing old contacts to auto-create
    CRM leads.
    """

    accounts = _connected_hosted_accounts(organization)
    if not accounts:
        raise HostedIgnoreSyncError(
            "No connected Hosted Account is available for this organization."
        )

    client = WhatsAppWebClient()
    staged_rows = []

    for account in accounts:
        try:
            payload = client.get_existing_chats(session_id=account.id)
        except WhatsAppWebGatewayError as exc:
            raise HostedIgnoreSyncError(str(exc)) from exc

        if not isinstance(payload, dict):
            raise HostedIgnoreSyncError(
                "Hosted gateway returned an invalid existing-chat response."
            )

        try:
            unresolved = int(payload.get("unresolved") or 0)
        except (TypeError, ValueError):
            unresolved = 0

        if unresolved:
            raise HostedIgnoreSyncError(
                "The Hosted gateway could not resolve every existing WhatsApp "
                f"chat to a phone number ({unresolved} unresolved). The current "
                "ignore list was kept unchanged; retry after the session is fully synced."
            )

        chats = payload.get("chats")
        if not isinstance(chats, list):
            raise HostedIgnoreSyncError(
                "Hosted gateway returned an invalid existing-chat list."
            )

        # One direct chat per phone is enough for the ignore rule.  Keep the
        # first useful row while still retaining the WhatsApp chat id for LID
        # fallback on future inbound messages.
        deduped = {}
        for item in chats:
            row = _normalize_gateway_chat(account=account, item=item)
            if not row:
                continue
            current = deduped.get(row.phone_number)
            if current is None or (not current.contact_name and row.contact_name):
                deduped[row.phone_number] = row

        staged_rows.extend(deduped.values())

    with transaction.atomic():
        HostedChatIgnoreContact.objects.filter(
            organization=organization,
        ).delete()
        if staged_rows:
            HostedChatIgnoreContact.objects.bulk_create(
                staged_rows,
                batch_size=1000,
            )

    return HostedIgnoreSyncResult(
        account_count=len(accounts),
        contact_count=len(staged_rows),
    )
