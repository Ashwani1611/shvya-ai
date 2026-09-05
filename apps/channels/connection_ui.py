"""Small UI wrappers for WhatsApp connection state.

Keep the normal WhatsApp views in views_flat.py. These wrappers control the
connection-aware landing behaviour and the Meta embedded-signup callback.
"""

import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from services.channels.embedded_signup_service import (
    EmbeddedSignupError,
    complete_embedded_signup,
)

from .models import WhatsAppAccount
from . import views_flat

logger = logging.getLogger(__name__)


def _has_connected_api_account(user):
    return WhatsAppAccount.objects.filter(
        organization=user.organization,
        connection_type=WhatsAppAccount.ConnectionType.API,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).exists()


def _inject_signup_toast(response):
    """Make Meta embedded-signup errors use the shared SHVYA toast UI."""
    content_type = response.get("Content-Type", "")
    if "text/html" not in content_type.lower() or getattr(response, "streaming", False):
        return response

    try:
        html = response.content.decode(response.charset or "utf-8")
    except (AttributeError, UnicodeDecodeError):
        return response

    marker = "</body>"
    if marker not in html.lower() or "shvya-whatsapp-signup-toast" in html:
        return response

    script = r'''
<script id="shvya-whatsapp-signup-toast">
(function () {
    if (typeof window.showSignupError !== 'function') return;
    var originalShowSignupError = window.showSignupError;
    window.showSignupError = function (message) {
        originalShowSignupError(message);
        if (typeof window.shvyaToast === 'function') {
            window.shvyaToast(
                message || 'WhatsApp connection failed. Please try again.',
                'error',
                {title: 'WhatsApp connection failed', duration: 6500}
            );
        }
    };
})();
</script>
'''

    index = html.lower().rfind(marker)
    html = html[:index] + script + html[index:]
    response.content = html.encode(response.charset or "utf-8")
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


@crm_login_required
def whatsapp_connect_api_view(request):
    """Show onboarding only when no active API account is connected."""
    if (
        request.method == "GET"
        and _has_connected_api_account(request.crm_user)
        and request.GET.get("add") != "1"
    ):
        return redirect("whatsapp-accounts")

    response = views_flat.whatsapp_connect_api_view(request)
    return _inject_signup_toast(response)


@crm_login_required
@require_POST
def whatsapp_embedded_signup_callback_view(request):
    """Finish Meta embedded signup and always persist a valid connection.

    Meta authorization/phone lookup failures are fatal. WABA webhook subscription
    failure is not: the connected number is still saved and shown in Connected
    Numbers, with a warning telling the admin to retry subscription later.
    """
    user = request.crm_user

    if not views_flat._admin_required(user):
        response = JsonResponse(
            {"error": "Only organization admins can connect WhatsApp."},
            status=403,
        )
        response["X-SHVYA-Toast"] = "off"
        return response

    code = (request.POST.get("code") or "").strip()
    waba_id = (request.POST.get("waba_id") or "").strip()
    phone_number_id = (request.POST.get("phone_number_id") or "").strip()

    if not code or not waba_id or not phone_number_id:
        response = JsonResponse(
            {
                "error": (
                    "Meta's popup didn't return all required WhatsApp details. "
                    "Please try the connection again."
                )
            },
            status=400,
        )
        response["X-SHVYA-Toast"] = "off"
        return response

    try:
        account, warning = complete_embedded_signup(
            organization=user.organization,
            code=code,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
        )
    except EmbeddedSignupError as exc:
        logger.warning(
            "Embedded signup failed for org %s: %s",
            user.organization_id,
            exc,
        )
        response = JsonResponse({"error": str(exc)}, status=502)
        response["X-SHVYA-Toast"] = "off"
        return response

    if warning:
        messages.warning(request, warning)
    else:
        messages.success(
            request,
            "WhatsApp Business API connected successfully.",
        )

    response = JsonResponse(
        {
            "redirect_url": reverse("whatsapp-accounts"),
            "account_id": str(account.id),
            "subscription_warning": warning,
        }
    )
    # The redirected page turns the Django message above into the final toast.
    response["X-SHVYA-Toast"] = "off"
    return response
