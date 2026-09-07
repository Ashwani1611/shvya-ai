import csv

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount
from apps.organizations.features import is_hosted_account_enabled
from apps.organizations.models import Organization
from services.channels.hosted_ignore_service import (
    HostedIgnoreSyncError,
    reset_hosted_ignore_list,
    sync_existing_hosted_chats,
)

from .models import AuditLog


def superuser_required(view_func):
    return user_passes_test(
        lambda user: user.is_authenticated and user.is_superuser,
        login_url="/superadmin/login/",
    )(view_func)


def _organization(organization_id):
    return get_object_or_404(Organization, pk=organization_id)


def _redirect_to_ignore_list(organization):
    return redirect(
        "superadmin-organization-hosted-ignore-list",
        organization_id=organization.id,
    )


@superuser_required
@require_GET
def organization_hosted_ignore_list_view(request, organization_id):
    organization = _organization(organization_id)
    hosted_accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            is_active=True,
        ).order_by("display_phone_number")
    )
    ignore_contacts = list(
        HostedChatIgnoreContact.objects.filter(
            organization=organization,
        )
        .select_related("account")
        .order_by("contact_name", "phone_number")
    )
    connected_count = sum(
        account.status == WhatsAppAccount.Status.CONNECTED
        for account in hosted_accounts
    )

    return render(
        request,
        "superadmin/hosted_ignore_list.html",
        {
            "organization": organization,
            "hosted_accounts": hosted_accounts,
            "connected_count": connected_count,
            "ignore_contacts": ignore_contacts,
            "ignore_count": len(ignore_contacts),
            "hosted_feature_enabled": is_hosted_account_enabled(organization),
        },
    )


@superuser_required
@require_POST
def organization_hosted_ignore_sync_view(request, organization_id):
    organization = _organization(organization_id)
    if not is_hosted_account_enabled(organization):
        messages.error(
            request,
            "Enable Hosted Account for this organization before syncing existing chats.",
        )
        return _redirect_to_ignore_list(organization)

    try:
        result = sync_existing_hosted_chats(organization=organization)
    except HostedIgnoreSyncError as exc:
        messages.error(request, f"Existing chat sync failed: {exc}")
        return _redirect_to_ignore_list(organization)

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
    )
    messages.success(
        request,
        (
            f"Ignore list refreshed from {result.account_count} connected Hosted "
            f"Account(s): {result.contact_count} existing direct chat(s) ignored."
        ),
    )
    return _redirect_to_ignore_list(organization)


@superuser_required
@require_POST
def organization_hosted_ignore_reset_view(request, organization_id):
    organization = _organization(organization_id)
    deleted = reset_hosted_ignore_list(organization=organization)

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
    )
    messages.success(
        request,
        (
            f"Ignore list reset. {deleted} row(s) removed. Future live messages "
            "from those numbers may auto-create leads again."
        ),
    )
    return _redirect_to_ignore_list(organization)


@superuser_required
@require_GET
def organization_hosted_ignore_download_view(request, organization_id):
    organization = _organization(organization_id)
    contacts = (
        HostedChatIgnoreContact.objects.filter(organization=organization)
        .select_related("account")
        .order_by("contact_name", "phone_number")
    )

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="hosted-ignore-list-{organization.id}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(
        [
            "Name",
            "Number",
            "Hosted Account",
            "Chat ID",
            "Synced At",
        ]
    )
    for contact in contacts:
        writer.writerow(
            [
                contact.contact_name,
                contact.phone_number,
                contact.account.display_phone_number,
                contact.chat_id,
                contact.synced_at.isoformat(),
            ]
        )
    return response
