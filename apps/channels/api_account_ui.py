from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required

from .models import WhatsAppAccount


@crm_login_required
@require_GET
def whatsapp_account_list_view(request):
    """Connected Numbers is the Meta API account list, never the Hosted list."""
    user = request.crm_user
    accounts = WhatsAppAccount.objects.filter(
        organization=user.organization,
        connection_type=WhatsAppAccount.ConnectionType.API,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).order_by("-updated_at")
    selected_owner = None
    owner_id = request.GET.get("owner", "").strip()
    if owner_id:
        selected_owner = User.objects.filter(
            id=owner_id,
            organization=user.organization,
            is_active=True,
        ).first()
        if selected_owner:
            from services.channels.hosted_whatsapp_service import (
                normalize_whatsapp_number,
                pipeline_whatsapp_number,
            )
            owner_numbers = {
                pipeline_whatsapp_number(pipeline)
                for pipeline in selected_owner.owned_pipelines.filter(is_active=True)
            }
            accounts = [
                account for account in accounts
                if normalize_whatsapp_number(
                    phone_number=account.display_phone_number or account.phone_number_id
                ) in owner_numbers
            ]
    return render(
        request,
        "channels/whatsapp_account_list.html",
        {
            "accounts": accounts,
            "can_manage": user.role == User.Role.ADMIN,
            "selected_owner": selected_owner,
        },
    )
