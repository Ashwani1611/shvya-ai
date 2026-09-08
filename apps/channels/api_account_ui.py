from django.conf import settings
from django.shortcuts import render
from django.views.decorators.http import require_GET

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
    ).order_by("-created_at")
    return render(
        request,
        "channels/whatsapp_account_list.html",
        {
            "accounts": accounts,
            "meta_app_id": settings.META_APP_ID,
            "meta_config_id": settings.META_CONFIG_ID,
        },
    )
