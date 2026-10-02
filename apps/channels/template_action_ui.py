"""Focused UI fixes for WhatsApp template submit, status, and CTA tracking."""

import json
import logging

from django.middleware.csrf import get_token
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from services.channels.template_cta_tracking import (
    template_has_trackable_cta,
    template_tracking_enabled,
)
from services.channels.template_delete_fix import delete_template as immediate_delete_template
from services.channels.template_meta_fix import (
    TemplateError,
    submit_template as meta_submit_template,
    sync_templates,
)

from . import template_ui
from .models import WhatsAppTemplate

logger = logging.getLogger(__name__)

# The active template UI module resolves these service functions from its
# module globals at request time. Wire submit/sync/delete endpoints and the
# create/edit flows through the compatibility fixes in one place.
template_ui.submit_template = meta_submit_template
template_ui.sync_templates = sync_templates
template_ui.delete_template = immediate_delete_template

# The template editor disables its action buttons during the submit event to
# prevent duplicate clicks. Disabled controls are excluded from the browser's
# form payload, so the clicked ``action=submit`` value can otherwise disappear
# before Django receives the POST. Preserve the clicked value in a hidden input
# before the existing duplicate-submit guard disables those buttons.
_ACTION_PRESERVER = b"""
<script data-shvya-template-action-preserver>
(function () {
  const form = document.getElementById('template-form');
  if (!form || form.querySelector('[data-template-action-value]')) return;

  const actionValue = document.createElement('input');
  actionValue.type = 'hidden';
  actionValue.name = 'action';
  actionValue.setAttribute('data-template-action-value', '');
  form.appendChild(actionValue);

  form.querySelectorAll('button[name="action"]').forEach((button) => {
    button.addEventListener('click', () => {
      actionValue.value = button.value;
    });
  });

  form.addEventListener('submit', (event) => {
    if (event.submitter && event.submitter.name === 'action') {
      actionValue.value = event.submitter.value;
    }
  }, true);
})();
</script>
"""


def _preserve_action(response):
    """Inject the action-preserver only into rendered template-editor HTML."""
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    content = response.content
    if b'data-shvya-template-action-preserver' in content:
        return response

    marker = b"</body>"
    if marker not in content:
        return response

    response.content = content.replace(marker, _ACTION_PRESERVER + marker, 1)
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


def _tracked_replacement_exists(template):
    prefix = f"{template.name[:138].rstrip('_')}_tracked"
    candidates = WhatsAppTemplate.objects.filter(
        organization_id=template.organization_id,
        account_id=template.account_id,
        name__startswith=prefix,
    ).select_related("meta_state")
    return any(template_tracking_enabled(candidate) for candidate in candidates)


def _tracking_upgrade_button(template):
    url = reverse("whatsapp-template-enable-click-tracking", args=[template.pk])
    return (
        f'<button type="button" data-track-url="{url}" '
        f'aria-label="Enable CTA tracking for {template.name}" '
        'title="Enable Website, Call and Copy Code tracking" '
        'class="action-track-cta flex h-9 w-9 items-center justify-center '
        'rounded-lg text-gray-400 hover:bg-purple-50 hover:text-purple-600">'
        '<i class="ti ti-pointer"></i></button>'
    ).encode("utf-8")


def _tracking_script(csrf_token):
    csrf = json.dumps(csrf_token)
    return f"""
<script data-shvya-cta-tracking-controls>
(function(){{
  const csrf={csrf};
  const toast=(message,type)=>typeof window.shvyaToast==='function'?window.shvyaToast(message,type):alert(message);
  document.querySelectorAll('.action-track-cta').forEach(button=>{{
    button.addEventListener('click',async()=>{{
      if(button.disabled)return;
      if(!confirm('Create and submit a tracked replacement template?\\n\\nThe current approved template stays unchanged. Use the replacement after Meta approves it.'))return;
      button.disabled=true;
      const original=button.innerHTML;
      button.innerHTML='<i class="ti ti-loader-2 animate-spin"></i>';
      try{{
        const response=await fetch(button.dataset.trackUrl,{{
          method:'POST',
          headers:{{'X-CSRFToken':csrf,'Accept':'application/json'}}
        }});
        const data=await response.json();
        if(!response.ok&&!data.ok)throw new Error(data.error||'CTA tracking could not be enabled.');
        toast(data.message||'CTA tracking replacement created.','success');
        if(data.status==='draft_needs_review'&&data.edit_url){{
          window.location.assign(data.edit_url);
          return;
        }}
        setTimeout(()=>window.location.reload(),900);
      }}catch(error){{
        toast(error.message||'CTA tracking could not be enabled.','error');
        button.disabled=false;
        button.innerHTML=original;
      }}
    }});
  }});
}})();
</script>
""".encode("utf-8")


def _inject_tracking_controls(response, request):
    """Add legacy-template upgrade controls without duplicating list markup."""

    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response
    content = response.content
    if b"data-shvya-cta-tracking-controls" in content:
        return response

    templates = (
        template_ui._templates(request.crm_user)
        .filter(
            status=WhatsAppTemplate.Status.APPROVED,
        )
        .exclude(meta_template_id="")
        .select_related("meta_state")
    )
    for template in templates:
        if (
            not template_has_trackable_cta(template)
            or template_tracking_enabled(template)
            or _tracked_replacement_exists(template)
        ):
            continue
        marker = (
            f'<button type="button" data-copy="{template.pk}"'.encode("utf-8")
        )
        if marker in content:
            content = content.replace(
                marker,
                _tracking_upgrade_button(template) + marker,
                1,
            )

    old_unavailable = b"""   }else if(!clickAvailable){
     clickNumber.textContent='\xe2\x80\x94';
     clickSub.textContent='Not returned by Meta';
   }else{"""
    new_unavailable = b"""   }else if(!clickAvailable){
     const upgradeAvailable=Boolean(data.cta_tracking&&data.cta_tracking.upgrade_available);
     clickNumber.textContent=upgradeAvailable?'\xe2\x80\x94':'0';
     clickSub.textContent=upgradeAvailable?'Enable SHVYA tracking for this legacy CTA':'No confirmed clicks tracked';
   }else{"""
    content = content.replace(old_unavailable, new_unavailable, 1)
    content = content.replace(
        b'<span class="text-[11px] text-gray-500">Meta-reported</span>',
        b'<span class="text-[11px] text-gray-500">Confirmed CTA activity</span>',
        1,
    )
    content = content.replace(
        b'\xc2\xb7 Data from Meta \xc2\xb7',
        b'\xc2\xb7 Meta and SHVYA receipts \xc2\xb7',
        1,
    )

    marker = b"</body>"
    if marker in content:
        content = content.replace(
            marker,
            _tracking_script(get_token(request)) + marker,
            1,
        )
    response.content = content
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(content))
    return response


def _clear_none_rejection_sentinel(user):
    """Remove Meta's ``NONE`` sentinel from already-synchronized templates."""
    WhatsAppTemplate.objects.filter(
        organization=user.organization,
        rejection_reason__iexact="NONE",
    ).update(rejection_reason="")


def _refresh_pending_templates(user):
    """Synchronize only accounts that currently have pending templates."""
    pending_account_ids = set(
        WhatsAppTemplate.objects.filter(
            organization=user.organization,
            status=WhatsAppTemplate.Status.PENDING,
        ).values_list("account_id", flat=True)
    )

    if not pending_account_ids:
        return

    for account in template_ui._accounts(user).filter(id__in=pending_account_ids):
        try:
            sync_templates(organization=user.organization, account=account)
        except TemplateError as exc:
            # A temporary Meta/API problem must not make the template list
            # unavailable. The existing manual Sync Templates action remains
            # available for an explicit retry and surfaces the API error.
            logger.warning(
                "Could not refresh pending WhatsApp templates for account %s: %s",
                account.id,
                exc,
            )


@crm_login_required
@require_GET
def template_list(request):
    """Refresh pending Meta templates before rendering their real status."""
    _clear_none_rejection_sentinel(request.crm_user)
    _refresh_pending_templates(request.crm_user)
    return _inject_tracking_controls(template_ui.template_list(request), request)


def template_create(request):
    return _preserve_action(template_ui.template_create(request))


def template_edit(request, template_id):
    return _preserve_action(template_ui.template_edit(request, template_id))
