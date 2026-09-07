"""Read/write helpers dedicated to the Meta WhatsApp API inbox.

Hosted Account conversations use a separate whatsapp-web.js transport and UI.
Nothing in this module is allowed to read or select a Hosted account.
"""

from django.db import models
from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead


API_CONNECTION_TYPE = WhatsAppAccount.ConnectionType.API


def _api_accounts(*, organization, connected_only=False):
    queryset = WhatsAppAccount.objects.filter(
        organization=organization,
        connection_type=API_CONNECTION_TYPE,
        is_active=True,
    )
    if connected_only:
        queryset = queryset.filter(status=WhatsAppAccount.Status.CONNECTED)
    return queryset


def list_api_accounts(*, organization, connected_only=True):
    return _api_accounts(
        organization=organization,
        connected_only=connected_only,
    )


def resolve_api_account_for_lead(*, organization, lead):
    """Resolve only a connected Meta/Cloud API account for this lead."""
    last_message = (
        WhatsAppMessage.objects.filter(
            organization=organization,
            lead=lead,
            account__connection_type=API_CONNECTION_TYPE,
            account__is_active=True,
            account__status=WhatsAppAccount.Status.CONNECTED,
        )
        .select_related("account")
        .order_by("-created_at", "-pk")
        .first()
    )
    if last_message:
        return last_message.account

    if lead.pipeline_id and lead.pipeline.phone_number:
        pipeline_phone = str(lead.pipeline.phone_number or "").strip()
        account = (
            _api_accounts(organization=organization, connected_only=True)
            .filter(
                models.Q(display_phone_number=pipeline_phone)
                | models.Q(phone_number_id=pipeline_phone)
            )
            .first()
        )
        if account:
            return account

    return _api_accounts(organization=organization, connected_only=True).first()


def is_within_api_24h_window(*, lead):
    """Return True only when a recent inbound exists on a Meta API account."""
    last_inbound = (
        WhatsAppMessage.objects.filter(
            lead=lead,
            account__connection_type=API_CONNECTION_TYPE,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    if not last_inbound:
        return False
    return (timezone.now() - last_inbound.created_at).total_seconds() < 24 * 3600


def list_api_conversations(*, organization, account=None, tab="all"):
    """Return conversation rows backed only by Meta WhatsApp API messages."""
    if account is not None and account.connection_type != API_CONNECTION_TYPE:
        account = None

    acc_q = Q(
        whatsapp_messages__organization=organization,
        whatsapp_messages__account__connection_type=API_CONNECTION_TYPE,
    )
    if account:
        acc_q &= Q(whatsapp_messages__account=account)

    base_msg_qs = WhatsAppMessage.objects.filter(
        organization=organization,
        lead__isnull=False,
        account__connection_type=API_CONNECTION_TYPE,
    )
    if account:
        base_msg_qs = base_msg_qs.filter(account=account)

    lead_ids = base_msg_qs.values_list("lead_id", flat=True).distinct()
    last_msg_qs = base_msg_qs.filter(lead=OuterRef("pk")).order_by(
        "-created_at",
        "-pk",
    )

    leads = (
        Lead.objects.filter(organization=organization, id__in=lead_ids)
        .annotate(
            last_message_at=Max(
                "whatsapp_messages__created_at",
                filter=acc_q,
            ),
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
            WhatsAppMessage.objects.filter(
                organization=organization,
                account__connection_type=API_CONNECTION_TYPE,
                bulk_recipient__isnull=False,
                **({"account": account} if account else {}),
            )
            .values_list("lead_id", flat=True)
            .distinct()
        )
        leads = leads.filter(id__in=broadcast_lead_ids)

    return leads


def get_api_conversation_messages(*, organization, lead, account=None):
    queryset = WhatsAppMessage.objects.filter(
        organization=organization,
        lead=lead,
        account__connection_type=API_CONNECTION_TYPE,
    )
    if account:
        if account.connection_type != API_CONNECTION_TYPE:
            return queryset.none()
        queryset = queryset.filter(account=account)
    return queryset.order_by("created_at", "pk")


def mark_api_conversation_read(*, organization, lead):
    return WhatsAppMessage.objects.filter(
        organization=organization,
        lead=lead,
        account__connection_type=API_CONNECTION_TYPE,
        direction=WhatsAppMessage.Direction.INBOUND,
        is_read=False,
    ).update(is_read=True)
