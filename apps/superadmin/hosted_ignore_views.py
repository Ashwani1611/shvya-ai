"""Superadmin controls for Hosted Account existing-chat ignore lists."""

import csv

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount
from apps.organizations.features import is_hosted_account_enabled
from apps.organizations.models import Organization
from services.channels.hosted_ignore_service import (
    HostedIgnoreSyncError,
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


def _redirect_to_list(organization):
    return redirect(
        "superadmin-organization-hosted-ignore-list",
        organization_id=organization.id,
    )


@superuser_required
def organization_hosted_ignore_list_view(request, organization_id):
    organization = _organization(organization_id)

    hosted_accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            is_active=True,
        )
        .annotate(ignore_count=Count("ignored_existing_chats"))
        .order_by("-connected_at")
    )

    ignore_contacts = (
        HostedChatIgnoreContact.objects.filter(organization=organization)
        .select_related("account")
        .order_by("contact_name", "phone_number", "account_id")
    )
    ignore_count = ignore_contacts.count()
    last_synced = (
        ignore_contacts.order_by("-synced_at")
        .values_list("synced_at", flat=True)
        .first()
    )
    page_obj = Paginator(ignore_contacts, 100).get_page(request.GET.get("page"))

    return render(
        request,
        "superadmin/hosted_ignore_list.html",
        {
            "organization": organization,
            "hosted_enabled": is_hosted_account_enabled(organization),
            "hosted_accounts": hosted_accounts,
            "connected_account_count": sum(
                1
                for account in hosted_accounts
                if account.status == WhatsAppAccount.Status.CONNECTED
            ),
            "ignore_count": ignore_count,
            "last_synced": last_synced,
            "page_obj": page_obj,
        },
    )


@superuser_required
@require_POST
def organization_hosted_ignore_sync_view(request, organization_id):
    organization = _organization(organization_id)

    if not is_hosted_account_enabled(organization):
        messages.error(
            request,
            "Hosted Account is disabled for this organization. Enable it before syncing existing chats.",
        )
        return _redirect_to_list(organization)

    try:
        result = sync_existing_hosted_chats(organization=organization)
    except HostedIgnoreSyncError as exc:
        messages.error(request, str(exc))
        return _redirect_to_list(organization)

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
        operation="hosted_existing_chat_ignore_sync",
        account_count=result.account_count,
        contact_count=result.contact_count,
    )

    messages.success(
        request,
        (
            f"Existing-chat ignore list synced successfully: "
            f"{result.contact_count} contact(s) from "
            f"{result.account_count} connected Hosted Account(s)."
        ),
    )
    return _redirect_to_list(organization)


@superuser_required
def organization_hosted_ignore_download_view(request, organization_id):
    organization = _organization(organization_id)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="hosted-ignore-{organization.id}.csv"'
    )

    writer = csv.writer(response)
    writer.writerow(["Name", "Number", "Hosted Account", "Chat ID", "Synced At"])

    queryset = (
        HostedChatIgnoreContact.objects.filter(organization=organization)
        .select_related("account")
        .order_by("contact_name", "phone_number", "account_id")
    )
    for item in queryset.iterator(chunk_size=1000):
        writer.writerow(
            [
                item.contact_name,
                item.phone_number,
                item.account.display_phone_number
                or item.account.phone_number_id
                or str(item.account_id),
                item.chat_id,
                item.synced_at.isoformat(),
            ]
        )

    return response


@superuser_required
@require_POST
def organization_hosted_ignore_reset_view(request, organization_id):
    organization = _organization(organization_id)
    deleted_count, _details = HostedChatIgnoreContact.objects.filter(
        organization=organization,
    ).delete()

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
        operation="hosted_existing_chat_ignore_reset",
        deleted_count=deleted_count,
    )

    messages.success(
        request,
        (
            f"Ignore list reset. {deleted_count} contact(s) removed. "
            "Those contacts can now follow the normal auto-lead rules until a new snapshot is synced."
        ),
    )
    return _redirect_to_list(organization)
