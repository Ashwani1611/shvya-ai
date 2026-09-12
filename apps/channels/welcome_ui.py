from django.contrib import messages
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from services.channels.hosted_whatsapp_service import get_pipeline_for_account

from .models import WhatsAppAccount, WhatsAppTemplate


@crm_login_required
@require_POST
def whatsapp_welcome_template_view(request, account_id):
    """Set the approved template used when a New Leads lead is created."""
    user = request.crm_user
    if user.role != User.Role.ADMIN:
        messages.error(request, "Only organization admins can change welcome messages.")
        return redirect("whatsapp-accounts")

    account = (
        WhatsAppAccount.objects.filter(
            id=account_id,
            organization=user.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        .select_related("organization")
        .first()
    )
    if not account:
        messages.error(request, "WhatsApp account not found.")
        return redirect("whatsapp-accounts")

    # Keep the same strict pipeline-number rule used by account automation.
    # Do not allow a template to be configured for an unlinked number.
    pipeline = get_pipeline_for_account(account=account)
    if not pipeline:
        messages.error(
            request,
            "Link this WhatsApp number to an active pipeline before setting a welcome template.",
        )
        return redirect("whatsapp-accounts")

    template_name = (request.POST.get("template_name") or "").strip()
    if not template_name:
        account.welcome_message = ""
        account.save(update_fields=["welcome_message", "updated_at"])
        messages.success(request, "Automatic welcome template cleared.")
        return redirect("whatsapp-accounts")

    template = (
        WhatsAppTemplate.objects.filter(
            organization=user.organization,
            account=account,
            name=template_name,
            status=WhatsAppTemplate.Status.APPROVED,
            attachment_type=WhatsAppTemplate.AttachmentType.NONE,
        )
        .exclude(meta_template_id="")
        .first()
    )
    if not template:
        messages.error(
            request,
            "Choose an approved text template from this WhatsApp connection.",
        )
        return redirect("whatsapp-accounts")

    account.welcome_message = template.name
    account.save(update_fields=["welcome_message", "updated_at"])
    messages.success(
        request,
        f"{template.name} will be sent automatically to new leads in the linked New Leads stage.",
    )
    return redirect("whatsapp-accounts")
