"""Repair actions for already-authorized Meta WhatsApp API accounts."""

import json
import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from services.channels.whatsapp_phone_registration import register_phone_number

from . import views_flat
from .models import WhatsAppAccount
from .providers.whatsapp import WhatsAppAPIError

logger = logging.getLogger(__name__)


def _meta_error_message(exc):
    body = getattr(exc, "response_body", None)
    if body:
        try:
            data = json.loads(body)
            error = data.get("error") or {}
            details = (error.get("error_data") or {}).get("details")
            return str(details or error.get("message") or exc)
        except (TypeError, ValueError):
            pass
    return str(exc)


@crm_login_required
@require_POST
def whatsapp_resubscribe_repair_view(request, account_id):
    """Register an API phone first, then repair its WABA subscription.

    Older SHVYA connections could save a Phone Number ID/WABA/token and mark
    the account connected without calling Meta's Cloud API ``/register``
    endpoint. The existing refresh button is the natural repair action for
    those accounts, so make it complete phone registration before it re-runs
    the webhook subscription.
    """
    user = request.crm_user

    if not views_flat._admin_required(user):
        messages.error(
            request,
            "Only organization admins can manage WhatsApp connections.",
        )
        return redirect("whatsapp-accounts")

    account = WhatsAppAccount.objects.filter(
        id=account_id,
        organization=user.organization,
    ).first()
    if not account:
        messages.error(request, "Account not found.")
        return redirect("whatsapp-accounts")

    if account.connection_type == WhatsAppAccount.ConnectionType.API:
        if not account.phone_number_id or not account.access_token:
            messages.error(
                request,
                "This WhatsApp API connection is missing its Phone Number ID or access token. Reconnect the number through Connect API.",
            )
            return redirect("whatsapp-accounts")

        try:
            register_phone_number(
                phone_number_id=account.phone_number_id,
                access_token=account.access_token,
            )
        except WhatsAppAPIError as exc:
            reason = _meta_error_message(exc)
            logger.warning(
                "Cloud API phone registration repair failed for account %s (org %s): %s",
                account.id,
                user.organization_id,
                reason,
            )
            messages.error(
                request,
                "WhatsApp phone registration is not active yet: " + reason,
            )
            return redirect("whatsapp-accounts")

    return views_flat.whatsapp_resubscribe_view(request, account_id)
