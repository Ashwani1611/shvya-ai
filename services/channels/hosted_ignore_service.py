"""Snapshot and enforce pre-existing Hosted WhatsApp chat ignore lists."""

from dataclasses import dataclass

from django.db import transaction

from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount
from apps.channels.providers.whatsapp_web import (
    WhatsAppWebClient,
    WhatsAppWebGatewayError,
)
from services.channels.hosted_whatsapp_service import normalize_whatsapp_number


class HostedIgnoreSyncError(ValueError):
    pass


@dataclass(frozen=True)
class HostedIgnoreSyncResult:
    account_count: int
    contact_count: int


def is_hosted_contact_ignored(*, account, phone_number):
    normalized = normalize_whatsapp_number(phone_number=phone_number)
    if not normalized:
        return False
    return HostedChatIgnoreContact.objects.filter(
        organization_id=account.organization_id,
        account_id=account.id,
        phone_number=normalized,
    ).exists()


def _connected_hosted_accounts(organization):
    return list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        ).order_by("connected_at")
    )


def _normalize_gateway_chat(*, account, chat):
    if not isinstance(chat, dict) or chat.get("isGroup"):
        return None

    phone_number = normalize_whatsapp_number(
        phone_number=chat.get("phoneNumber") or chat.get("number") or ""
    )
    if not phone_number:
        return None

    own_number = normalize_whatsapp_number(
        phone_number=account.display_phone_number or account.phone_number_id
    )
    if own_number and phone_number == own_number:
        return None

    contact_name = str(
        chat.get("contactName") or chat.get("name") or phone_number
    ).strip()[:180]
    chat_id = str(chat.get("chatId") or "").strip()[:160]
    return {
        "phone_number": phone_number,
        "contact_name": contact_name,
        "chat_id": chat_id,
    }


def sync_existing_hosted_chats(*, organization):
    """Replace ignore snapshots for every currently connected Hosted Account.

    Gateway reads happen before any database delete. If one account cannot be
    read, the existing ignore list is left untouched.
    """
    accounts = _connected_hosted_accounts(organization)
    if not accounts:
        raise HostedIgnoreSyncError(
            "No connected Hosted Account is available for this organization."
        )

    client = WhatsAppWebClient()
    snapshots = {}
    try:
        for account in accounts:
            response = client.get_existing_chats(session_id=account.id)
            raw_chats = response.get("chats", []) if isinstance(response, dict) else []
            if not isinstance(raw_chats, list):
                raise HostedIgnoreSyncError(
                    f"Hosted Account {account.display_phone_number or account.id} "
                    "returned an invalid chat list."
                )

            deduped = {}
            for chat in raw_chats:
                normalized = _normalize_gateway_chat(account=account, chat=chat)
                if not normalized:
                    continue
                deduped[normalized["phone_number"]] = normalized
            snapshots[account.id] = list(deduped.values())
    except WhatsAppWebGatewayError as exc:
        raise HostedIgnoreSyncError(str(exc)) from exc

    contact_count = 0
    with transaction.atomic():
        for account in accounts:
            HostedChatIgnoreContact.objects.filter(
                organization=organization,
                account=account,
            ).delete()
            rows = [
                HostedChatIgnoreContact(
                    organization=organization,
                    account=account,
                    phone_number=item["phone_number"],
                    contact_name=item["contact_name"],
                    chat_id=item["chat_id"],
                )
                for item in snapshots[account.id]
            ]
            HostedChatIgnoreContact.objects.bulk_create(rows)
            contact_count += len(rows)

    return HostedIgnoreSyncResult(
        account_count=len(accounts),
        contact_count=contact_count,
    )


def reset_hosted_ignore_list(*, organization):
    deleted, _details = HostedChatIgnoreContact.objects.filter(
        organization=organization
    ).delete()
    return deleted
