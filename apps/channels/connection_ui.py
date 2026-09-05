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
    """Add robust Meta embedded-signup completion and shared toast errors.

    Meta has emitted WA_EMBEDDED_SIGNUP postMessage payloads as both JSON
    strings and already-parsed objects. The template's original listener only
    accepts the string form. If Meta sends an object, the popup can finish
    successfully while SHVYA never receives the WABA/phone IDs, so the backend
    callback is never called and Connected Numbers never refreshes.

    This compatibility listener accepts both payload shapes and feeds the
    existing signupData/maybeFinishSignup flow. It is intentionally injected
    here so older cached templates also get the fix after deployment.
    """
    content_type = response.get("Content-Type", "")
    if "text/html" not in content_type.lower() or getattr(response, "streaming", False):
        return response

    try:
        html = response.content.decode(response.charset or "utf-8")
    except (AttributeError, UnicodeDecodeError):
        return response

    marker = "</body>"
    if marker not in html.lower() or "shvya-whatsapp-signup-bridge" in html:
        return response

    script = r'''
<script id="shvya-whatsapp-signup-bridge">
(function () {
    // Keep the existing inline error box, but also surface failures as a toast.
    if (typeof window.showSignupError === 'function') {
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
    }

    // Meta may post either a JSON string or an object depending on SDK/browser.
    // The page's original handler parses strings; this bridge covers objects
    // (and harmlessly re-applies string FINISH events) so signup always reaches
    // SHVYA's server callback once all three values are available.
    window.addEventListener('message', function (event) {
        if (!event.origin || !event.origin.endsWith('facebook.com')) return;

        var data = event.data;
        if (typeof data === 'string') {
            try { data = JSON.parse(data); } catch (e) { return; }
        }
        if (!data || typeof data !== 'object') return;
        if (data.type !== 'WA_EMBEDDED_SIGNUP') return;

        if (data.event === 'FINISH' && data.data) {
            if (typeof window.signupData === 'undefined') return;
            window.signupData.waba_id = data.data.waba_id || window.signupData.waba_id;
            window.signupData.phone_number_id = data.data.phone_number_id || window.signupData.phone_number_id;
            if (typeof window.maybeFinishSignup === 'function') {
                window.maybeFinishSignup();
            }
        }
    });
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
            "ok": True,
            "redirect_url": reverse("whatsapp-accounts"),
            "account_id": str(account.id),
            "phone_number": account.display_phone_number or "",
            "business_name": account.business_name or "",
            "subscription_warning": warning,
        }
    )
    # The browser redirects immediately to Connected Numbers. That fresh GET
    # reads the persisted account from PostgreSQL, so no stale client-side list
    # needs to be manually refreshed.
    response["X-SHVYA-Toast"] = "off"
    response["Cache-Control"] = "no-store"
    return response
