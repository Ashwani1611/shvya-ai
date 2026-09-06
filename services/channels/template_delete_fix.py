"""Immediate-delete compatibility for WhatsApp message templates.

A successful Meta delete should remove the local template immediately instead
of leaving a ``pending_deletion`` row behind. Failed remote deletes still keep
the local record intact so the user can retry safely.
"""

from django.db import transaction

from . import template_service as base

TemplateError = base.TemplateError


def delete_template(*, template):
    """Delete the template locally as soon as Meta confirms it is gone."""
    if not template.meta_template_id:
        with transaction.atomic():
            base._audit(
                template,
                base.WhatsAppTemplateOperation.Operation.DELETE,
                True,
                payload={"local_draft": True},
            )
            template.delete()
        return

    account = template.account
    url = f"{base.meta.GRAPH_API_BASE}/{account.waba_id}/message_templates"
    try:
        response = base.meta.requests.delete(
            url,
            headers={"Authorization": f"Bearer {account.access_token}"},
            params={"name": template.name, "hsm_id": template.meta_template_id},
            timeout=base.meta.REQUEST_TIMEOUT_SECONDS,
        )
    except base.meta.requests.RequestException as exc:
        raise TemplateError(f"Network error deleting WhatsApp template: {exc}") from exc

    # A 404 means the template is already absent on Meta and is therefore safe
    # to remove locally as well. Other Meta failures must leave the local row
    # untouched so the user can see it and retry.
    if not response.ok and response.status_code != 404:
        err = base._error(
            base.WhatsAppAPIError(
                "WhatsApp template deletion failed.",
                response.status_code,
                response.text,
            )
        )
        base._audit(
            template,
            base.WhatsAppTemplateOperation.Operation.DELETE,
            False,
            error=err,
        )
        raise TemplateError(
            err["message"],
            status_code=err["http_status"],
            meta_error_code=err["code"],
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {"deleted": True}

    with transaction.atomic():
        base._audit(
            template,
            base.WhatsAppTemplateOperation.Operation.DELETE,
            True,
            payload=payload,
        )
        # WhatsAppTemplateMetadata cascades, while the operation audit uses
        # SET_NULL, so the successful delete remains auditable after removal.
        template.delete()
