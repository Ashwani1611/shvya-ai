"""Management wrappers for Hosted WhatsApp account lifecycle UI."""

import re

from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from services.channels.hosted_whatsapp_service import HOSTED_CONNECTION_TYPE

from . import hosted_attachment_ui, hosted_chat_ui, hosted_ui
from .hosted_lifecycle import delete_hosted_account
from .hosted_tasks import initialize_hosted_session_task
from .models import WhatsAppAccount


_LOGOUT_BUTTON_RE = re.compile(
    rb'(?P<button><button type="button" class="hosted-action js-logout-session"'
    rb'[^>]*data-url="(?P<url>[^"]+/logout/)"[^>]*>'
    rb'<i class="ti ti-logout"></i></button>)'
)

_MANAGEMENT_SCRIPT = b"""
<script data-hosted-account-management>
(function(){
  const csrf=document.querySelector('input[name=csrfmiddlewaretoken]')?.value||'';

  const requestedLogin=new URLSearchParams(window.location.search).get('login');
  if(requestedLogin){
    const qrButton=Array.from(document.querySelectorAll('.js-get-qr')).find(
      button=>String(button.dataset.account||'')===requestedLogin
    );
    if(qrButton)setTimeout(()=>qrButton.click(),0);
  }

  document.querySelectorAll('.js-delete-hosted-account').forEach(button=>{
    button.addEventListener('click',async()=>{
      const confirmed=confirm(
        'Permanently delete this Hosted WhatsApp account? Hosted chat history and account-specific follow-up automation will also be removed.'
      );
      if(!confirmed)return;
      button.disabled=true;
      try{
        const response=await fetch(button.dataset.url,{
          method:'POST',
          headers:{'X-CSRFToken':csrf,'Content-Type':'application/json'},
          body:'{}'
        });
        const result=await response.json();
        if(!response.ok)throw new Error(result.error||'Could not delete Hosted WhatsApp account.');
        location.reload();
      }catch(error){
        alert(error.message||'Could not delete Hosted WhatsApp account.');
        button.disabled=false;
      }
    });
  });
})();
</script>
"""


def _inject_management_controls(response, *, can_delete):
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    content = response.content
    if can_delete and b"js-delete-hosted-account" not in content:
        def replace_logout(match):
            logout_url = match.group("url")
            delete_url = logout_url[:-len(b"logout/")] + b"delete/"
            delete_button = (
                b'<button type="button" class="hosted-action js-delete-hosted-account" '
                b'title="Delete Hosted Account" aria-label="Delete Hosted Account" data-url="'
                + delete_url
                + b'"><i class="ti ti-trash"></i></button>'
            )
            return match.group("button") + delete_button

        content = _LOGOUT_BUTTON_RE.sub(replace_logout, content)

    if b"data-hosted-account-management" not in content and b"</body>" in content:
        content = content.replace(b"</body>", _MANAGEMENT_SCRIPT + b"</body>", 1)

    response.content = content
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(content))
    return response


@crm_login_required
@require_GET
def whatsapp_connect_hosted_view(request):
    response = hosted_ui.whatsapp_connect_hosted_view(request)
    user = getattr(request, "crm_user", None)
    return _inject_management_controls(
        response,
        can_delete=bool(user and user.role == User.Role.ADMIN),
    )


@crm_login_required
@require_GET
def hosted_session_chats_view(request, account_id):
    """Open chats only for a live Hosted session; otherwise reopen QR login."""
    account = (
        WhatsAppAccount.objects.select_related("organization")
        .filter(
            id=account_id,
            organization=request.crm_user.organization,
            connection_type=HOSTED_CONNECTION_TYPE,
            is_active=True,
        )
        .first()
    )
    if not account:
        raise Http404

    # A stale DB disconnect can happen if a ready callback was lost. Preserve
    # the existing self-healing check before deciding that the user must login.
    hosted_chat_ui._repair_live_status(account)
    account.refresh_from_db(fields=["status"])

    if account.status != WhatsAppAccount.Status.CONNECTED:
        if account.status != WhatsAppAccount.Status.PENDING:
            account.status = WhatsAppAccount.Status.PENDING
            account.save(update_fields=["status", "updated_at"])
        initialize_hosted_session_task.delay(str(account.id))
        return redirect(
            f"{reverse('whatsapp-connect-hosted')}?login={account.id}"
        )

    return hosted_attachment_ui.hosted_session_chats_view(request, account_id)


@crm_login_required
@require_POST
def hosted_session_delete_view(request, account_id):
    user = request.crm_user
    if user.role != User.Role.ADMIN:
        return JsonResponse(
            {"ok": False, "error": "Only organization admins can delete Hosted WhatsApp accounts."},
            status=403,
        )

    account = (
        WhatsAppAccount.objects.select_related("organization")
        .filter(
            id=account_id,
            organization=user.organization,
            connection_type=HOSTED_CONNECTION_TYPE,
            is_active=True,
        )
        .first()
    )
    if not account:
        raise Http404

    result = delete_hosted_account(account=account)
    return JsonResponse({"ok": True, **result})
