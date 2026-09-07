"""Read/send helpers for the Meta WhatsApp Cloud API inbox only.

The public `/dashboard/whatsapp/chats/` inbox is intentionally isolated from
Hosted linked-device sessions. Hosted conversations use
`services.channels.hosted_chat_service` and the `/connect/hosted/.../chats/`
views instead.
"""

from django.db import models
from django.db.models import Count, Max, OuterRef, Q, Subquery

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead


API_CONNECTION_TYPE = WhatsAppAccount.ConnectionType.API


def _api_messages(*, organization):
    return WhatsAppMessage.objects.filter(
        organization=organization,
        account__connection_type=API_CONNECTION_TYPE,
    )


def list_api_conversations(*, organization, account=None, tab="all"):
    """Return conversation rows built only from Meta API account messages."""
    if account and account.connection_type != API_CONNECTION_TYPE:
        return Lead.objects.none()

    acc_q = Q(
        whatsapp_messages__organization=organization,
        whatsapp_messages__account__connection_type=API_CONNECTION_TYPE,
    )
    if account:
        acc_q &= Q(whatsapp_messages__account=account)

    base_msg_qs = _api_messages(organization=organization).filter(lead__isnull=False)
    if account:
        base_msg_qs = base_msg_qs.filter(account=account)

    lead_ids = base_msg_qs.values_list("lead_id", flat=True).distinct()
    last_msg_qs = base_msg_qs.filter(lead=OuterRef("pk")).order_by(
        "-created_at", "-pk"
    )

    leads = (
        Lead.objects.filter(organization=organization, id__in=lead_ids)
        .annotate(
            last_message_at=Max("whatsapp_messages__created_at", filter=acc_q),
            unread_count=Count(
                "whatsapp_messages",
                filter=(
                    Q(
                        whatsapp_messages__direction=WhatsAppMessage.Direction.INBOUND,
                        whatsapp_messages__is_read=False,
                    )
                    & acc_q
                ),
            ),
            failed_count=Count(
                "whatsapp_messages",
                filter=(
                    Q(whatsapp_messages__status=WhatsAppMessage.Status.FAILED)
                    & acc_q
                ),
            ),
        )
        .annotate(
            last_msg_body=Subquery(last_msg_qs.values("body")[:1]),
            last_msg_direction=Subquery(last_msg_qs.values("direction")[:1]),
            last_msg_status=Subquery(last_msg_qs.values("status")[:1]),
            last_msg_error=Subquery(last_msg_qs.values("error")[:1]),
        )
        .order_by("-last_message_at")
    )

    if tab == "unread":
        leads = leads.filter(unread_count__gt=0)
    elif tab == "needs_reply":
        leads = leads.filter(last_msg_direction=WhatsAppMessage.Direction.INBOUND)
    elif tab == "failed":
        leads = leads.filter(last_msg_status=WhatsAppMessage.Status.FAILED)
    elif tab == "broadcasts":
        broadcast_lead_ids = (
            _api_messages(organization=organization)
            .filter(
                bulk_recipient__isnull=False,
                **({"account": account} if account else {}),
            )
            .values_list("lead_id", flat=True)
            .distinct()
        )
        leads = leads.filter(id__in=broadcast_lead_ids)

    return leads


def get_api_conversation_messages(*, organization, lead, account=None):
    messages = _api_messages(organization=organization).filter(lead=lead)
    if account:
        if account.connection_type != API_CONNECTION_TYPE:
            return messages.none()
        messages = messages.filter(account=account)
    return messages.order_by("created_at")


def mark_api_conversation_read(*, organization, lead, account=None):
    messages = _api_messages(organization=organization).filter(
        lead=lead,
        direction=WhatsAppMessage.Direction.INBOUND,
        is_read=False,
    )
    if account:
        if account.connection_type != API_CONNECTION_TYPE:
            return 0
        messages = messages.filter(account=account)
    return messages.update(is_read=True)


def connected_api_accounts(*, organization):
    return WhatsAppAccount.objects.filter(
        organization=organization,
        connection_type=API_CONNECTION_TYPE,
        is_active=True,
        status=WhatsAppAccount.Status.CONNECTED,
    )


def resolve_api_account_for_lead(*, organization, lead):
    """Resolve an outbound account without ever falling back to Hosted."""
    last_message = (
        _api_messages(organization=organization)
        .filter(lead=lead)
        .select_related("account")
        .order_by("-created_at")
        .first()
    )
    if (
        last_message
        and last_message.account.is_active
        and last_message.account.status == WhatsAppAccount.Status.CONNECTED
    ):
        return last_message.account

    accounts = connected_api_accounts(organization=organization)
    if lead.pipeline_id and lead.pipeline.phone_number:
        pipeline_number = str(lead.pipeline.phone_number or "").strip()
        account = accounts.filter(
            models.Q(display_phone_number=pipeline_number)
            | models.Q(phone_number_id=pipeline_number)
        ).first()
        if account:
            return account

    return accounts.first()
