"""UI wrappers for WhatsApp template editor form submission.

The template editor disables its action buttons during the submit event to
prevent duplicate clicks. Disabled controls are excluded from the browser's
form payload, so the clicked ``action=submit`` value can otherwise disappear
before Django receives the POST. These wrappers inject a tiny client-side
preserver into editor responses while leaving the existing template lifecycle
views and service layer unchanged.
"""

from . import template_ui

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


def template_create(request):
    return _preserve_action(template_ui.template_create(request))


def template_edit(request, template_id):
    return _preserve_action(template_ui.template_edit(request, template_id))
