"""Tenant-scoped WhatsApp presentation shared by Teams and its settings action."""

from django.db.models import Exists, OuterRef, Subquery

from apps.channels.connection_attempts import WhatsAppConnectionAttempt
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline
from services.channels.hosted_whatsapp_service import (
    normalize_whatsapp_number,
    pipeline_whatsapp_number,
)


def _account_number(account):
    # A Meta phone_number_id identifies a Graph object, not a telephone number.
    # Only Hosted accounts store the actual number in that legacy field.
    phone = account.display_phone_number
    if not phone and account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        phone = account.phone_number_id
    return normalize_whatsapp_number(phone_number=phone)


def _account_label(account):
    if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        return "Hosted Account"
    if (
        account.latest_connection_stage in {"coexistence_connected", "coexistence_sync_warning"}
        or account.has_coexistence_messages
    ):
        return "WhatsApp Coexistence"
    return "WhatsApp API"


def member_whatsapp_connections(*, organization, members):
    """Resolve at most one current connection for each member, in two queries.

    Ownership comes from active pipelines, never from another org's accounts or
    a contact-number coincidence. The member's phone disambiguates multiple
    owned numbers; without a match, a single connected number is unambiguous.
    Multiple remaining numbers require selection rather than guessing.

    API and Business App Coexistence share the API transport. Distinguish them
    using a successful onboarding marker or existing Coexistence-only messages,
    not the legacy ConnectionType.coexisted enum (which means Hosted).
    """
    members_by_id = {
        member.pk: member
        for member in members
        if member.organization_id == organization.pk
    }
    results = {
        member_id: {
            "account": None,
            "number": "",
            "label": "Not connected",
            "candidates": (),
            "reason": "No connected WhatsApp number is linked to an active pipeline owned by this member.",
        }
        for member_id in members_by_id
    }
    if not members_by_id:
        return results

    numbers_by_member = {member_id: set() for member_id in members_by_id}
    for pipeline in Pipeline.objects.filter(
        organization=organization,
        owner_id__in=members_by_id,
        is_active=True,
    ).only("owner_id", "country_code", "phone_number"):
        number = pipeline_whatsapp_number(pipeline)
        if number:
            numbers_by_member[pipeline.owner_id].add(number)

    latest_attempt = WhatsAppConnectionAttempt.objects.filter(
        organization=organization,
        account_id=OuterRef("pk"),
        status=WhatsAppConnectionAttempt.Status.CONNECTED,
    ).order_by("-updated_at", "-created_at", "-pk")
    coexistence_messages = WhatsAppMessage.objects.filter(
        organization=organization,
        account_id=OuterRef("pk"),
        media_payload__coexistence_sync=True,
    )
    accounts = (
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type__in=(
                WhatsAppAccount.ConnectionType.API,
                WhatsAppAccount.ConnectionType.coexisted,
            ),
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        # Teams never needs to fetch/decrypt credentials or load message bodies.
        .only(
            "id", "organization_id", "connection_type", "display_phone_number",
            "phone_number_id", "business_name", "updated_at", "connected_at",
        )
        .annotate(
            latest_connection_stage=Subquery(latest_attempt.values("stage")[:1]),
            has_coexistence_messages=Exists(coexistence_messages),
        )
        .order_by("-updated_at", "-connected_at", "-pk")
    )
    accounts_by_number = {}
    for account in accounts:
        number = _account_number(account)
        if number and number not in accounts_by_number:
            account.team_phone_number = number
            account.team_connection_label = _account_label(account)
            accounts_by_number[number] = account

    for member_id, member in members_by_id.items():
        owned_numbers = numbers_by_member[member_id]
        candidates = tuple(
            accounts_by_number[number]
            for number in sorted(owned_numbers)
            if number in accounts_by_number
        )
        result = results[member_id]
        result["candidates"] = candidates
        member_phone = normalize_whatsapp_number(phone_number=member.phone)
        if member_phone and member_phone in owned_numbers:
            # Do not silently substitute another pipeline when the member's
            # explicitly matching number is disconnected.
            selected = accounts_by_number.get(member_phone)
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            selected = None
            if len(candidates) > 1:
                result["reason"] = (
                    "Multiple pipeline numbers are connected. Set the member's phone "
                    "to the intended pipeline number to select one connection."
                )
        if selected is not None:
            result.update(
                account=selected,
                number=selected.team_phone_number,
                label=selected.team_connection_label,
                reason="",
            )
    return results
