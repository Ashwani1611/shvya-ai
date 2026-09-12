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


def _connected_identity_values(*, organization, account=None):
    """Return identifiers that belong to currently connected Meta API numbers.

    Reconnecting the same Meta number can create or reactivate a different
    ``WhatsAppAccount`` row while historical ``WhatsAppMessage`` rows remain
    attached to the older account row. The inbox should keep that history when
    the same number is connected again, but must still hide chats for numbers
    that are genuinely removed/disconnected.
    """
    connected = _api_accounts(organization=organization, connected_only=True)
    if account is not None:
        if (
            account.connection_type != API_CONNECTION_TYPE
            or not account.is_active
            or account.status != WhatsAppAccount.Status.CONNECTED
            or account.organization_id != organization.id
        ):
            return set(), set(), set()
        connected = connected.filter(pk=account.pk)

    account_ids = set(connected.values_list("id", flat=True))
    phone_number_ids = {
        value
        for value in connected.values_list("phone_number_id", flat=True)
        if value
    }
    display_numbers = {
        value
        for value in connected.values_list("display_phone_number", flat=True)
        if value
    }
    return account_ids, phone_number_ids, display_numbers


def _visible_message_account_q(*, organization, account=None, prefix=""):
    """Build a Q matching API message accounts represented by a live number."""
    account_ids, phone_number_ids, display_numbers = _connected_identity_values(
        organization=organization,
        account=account,
    )
    identity_q = Q()
    if account_ids:
        identity_q |= Q(**{f"{prefix}account_id__in": account_ids})
    if phone_number_ids:
        identity_q |= Q(**{f"{prefix}account__phone_number_id__in": phone_number_ids})
    if display_numbers:
        identity_q |= Q(**{f"{prefix}account__display_phone_number__in": display_numbers})

    if not (account_ids or phone_number_ids or display_numbers):
        return Q(pk__in=[])

    return (
        Q(**{f"{prefix}organization": organization})
        & Q(**{f"{prefix}account__connection_type": API_CONNECTION_TYPE})
        & identity_q
    )


def resolve_api_account_for_lead(*, organization, lead):
    """Resolve only a connected Meta/Cloud API account for this lead."""
    connected_accounts = _api_accounts(
        organization=organization,
        connected_only=True,
    )

    # Prefer the currently connected account representing the number this lead
    # already used, even when the most recent message belongs to an older row
    # for that same Meta number.
    last_message = (
        WhatsAppMessage.objects.filter(
            organization=organization,
            lead=lead,
            account__connection_type=API_CONNECTION_TYPE,
        )
        .select_related("account")
        .order_by("-created_at", "-pk")
        .first()
    )
    if last_message:
        prior = last_message.account
        account = connected_accounts.filter(
            models.Q(phone_number_id=prior.phone_number_id)
            if prior.phone_number_id
            else models.Q(pk=prior.pk)
        ).first()
        if account:
            return account
        if prior.display_phone_number:
            account = connected_accounts.filter(
                display_phone_number=prior.display_phone_number
            ).first()
            if account:
                return account

    if lead.pipeline_id and lead.pipeline.phone_number:
        pipeline_phone = str(lead.pipeline.phone_number or "").strip()
        account = (
            connected_accounts.filter(
                models.Q(display_phone_number=pipeline_phone)
                | models.Q(phone_number_id=pipeline_phone)
            )
            .first()
        )
        if account:
            return account

    return connected_accounts.first()


def is_within_api_24h_window(*, lead):
    """Return True when a recent inbound belongs to a currently connected API number."""
    organization = lead.organization
    visible_q = _visible_message_account_q(organization=organization)
    last_inbound = (
        WhatsAppMessage.objects.filter(
            visible_q,
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    if not last_inbound:
        return False
    return (timezone.now() - last_inbound.created_at).total_seconds() < 24 * 3600


def list_api_conversations(*, organization, account=None, tab="all"):
    """Return chats for Meta API numbers that are currently connected.

    Historical messages from a prior account row remain visible when the same
    Meta number is connected again. Messages for numbers with no live API
    account remain excluded.
    """
    if account is not None and (
        account.connection_type != API_CONNECTION_TYPE
        or not account.is_active
        or account.status != WhatsAppAccount.Status.CONNECTED
        or account.organization_id != organization.id
    ):
        account = None

    base_msg_qs = WhatsAppMessage.objects.filter(
        _visible_message_account_q(
            organization=organization,
            account=account,
        ),
        lead__isnull=False,
    )

    acc_q = _visible_message_account_q(
        organization=organization,
        account=account,
        prefix="whatsapp_messages__",
    )

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
        broadcast_lead_ids = base_msg_qs.filter(
            bulk_recipient__isnull=False,
        ).values_list("lead_id", flat=True).distinct()
        leads = leads.filter(id__in=broadcast_lead_ids)

    return leads


def get_api_conversation_messages(*, organization, lead, account=None):
    queryset = WhatsAppMessage.objects.filter(
        _visible_message_account_q(
            organization=organization,
            account=account,
        ),
        lead=lead,
    )
    return queryset.order_by("created_at", "pk")


def mark_api_conversation_read(*, organization, lead):
    return WhatsAppMessage.objects.filter(
        _visible_message_account_q(organization=organization),
        lead=lead,
        direction=WhatsAppMessage.Direction.INBOUND,
        is_read=False,
    ).update(is_read=True)
