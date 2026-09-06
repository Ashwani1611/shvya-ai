"""Focused UI fixes for WhatsApp template submit and status refresh."""

import logging

from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from services.channels.template_service import TemplateError, sync_templates

from . import template_ui
from .models import WhatsAppTemplate

logger = logging.getLogger(__name__)

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


@crm_login_required
@require_GET
def template_list(request):
    """Refresh only pending Meta templates before rendering their real status."""
    user = request.crm_user
    pending_account_ids = set(
        WhatsAppTemplate.objects.filter(
            organization=user.organization,
            status=WhatsAppTemplate.Status.PENDING,
        ).values_list("account_id", flat=True)
    )

    if pending_account_ids:
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

    return template_ui.template_list(request)


def template_create(request):
    return _preserve_action(template_ui.template_create(request))


def template_edit(request, template_id):
    return _preserve_action(template_ui.template_edit(request, template_id))
