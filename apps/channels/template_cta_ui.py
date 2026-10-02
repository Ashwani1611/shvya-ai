"""Tenant-safe UI action for upgrading legacy native CTAs to tracked CTAs."""

from __future__ import annotations

import copy

from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from services.channels.template_cta_tracking import (
    source_buttons_for_template,
    template_has_trackable_cta,
    template_tracking_enabled,
)
from services.channels.template_meta_fix import submit_template
from services.channels.template_service import TemplateError, copy_template, state_for

from . import template_ui
from .models import WhatsAppTemplate
from .template_models import WhatsAppTemplateMetadata


def _unique_name(template):
    base = f"{template.name[:138].rstrip('_')}_tracked"
    candidate = base
    counter = 2
    while WhatsAppTemplate.objects.filter(
        account_id=template.account_id,
        name=candidate,
    ).exists():
        suffix = f"_{counter}"
        candidate = f"{base[:150-len(suffix)]}{suffix}"
        counter += 1
    return candidate


def _copy_delivery_state(*, source, copied):
    source_state = state_for(source)
    copied_state = WhatsAppTemplateMetadata.objects.select_for_update().get(
        template=copied
    )
    copied_state.delivery_bindings = copy.deepcopy(
        source_state.delivery_bindings or {}
    )
    copied_state.delivery_media = copy.deepcopy(source_state.delivery_media or {})
    copied_state.header_sample_handle = source_state.header_sample_handle
    copied_state.header_file_name = source_state.header_file_name
    copied_state.header_mime_type = source_state.header_mime_type
    copied_state.header_file_size = source_state.header_file_size
    if source.template_format == WhatsAppTemplate.Format.CAROUSEL:
        # A tracking replacement is the same approved visual template with CTA
        # destinations changed. Reusing the source approval sample handles keeps
        # the one-click upgrade possible without re-uploading identical media.
        copied_state.carousel_config = copy.deepcopy(
            source_state.carousel_config or {}
        )
    copied_state.save(
        update_fields=[
            "delivery_bindings",
            "delivery_media",
            "header_sample_handle",
            "header_file_name",
            "header_mime_type",
            "header_file_size",
            "carousel_config",
            "updated_at",
        ]
    )


@crm_login_required
@require_POST
def enable_template_cta_tracking(request, template_id):
    user = request.crm_user
    if not template_ui._admin(user):
        return JsonResponse(
            {"error": "Only organization admins can enable CTA tracking."},
            status=403,
        )

    source = template_ui._template(user, template_id)
    if source is None:
        return JsonResponse({"error": "Template not found."}, status=404)
    if source.status != WhatsAppTemplate.Status.APPROVED or not source.meta_template_id:
        return JsonResponse(
            {"error": "Only approved, synchronized templates can be upgraded."},
            status=400,
        )
    if template_tracking_enabled(source):
        return JsonResponse(
            {
                "ok": True,
                "status": "already_enabled",
                "message": "SHVYA CTA tracking is already enabled for this template.",
            }
        )
    if not template_has_trackable_cta(source):
        return JsonResponse(
            {"error": "This template has no Website, Call, or Copy Code CTA to upgrade."},
            status=400,
        )

    with transaction.atomic():
        copied = copy_template(template=source, created_by=user)
        copied.name = _unique_name(source)
        if copied.template_format == WhatsAppTemplate.Format.STANDARD:
            copied.buttons = source_buttons_for_template(source)
        copied.save(update_fields=["name", "buttons", "updated_at"])
        _copy_delivery_state(source=source, copied=copied)

    edit_url = reverse("whatsapp-template-edit", args=[copied.pk])
    try:
        submit_template(template=copied)
    except TemplateError as exc:
        # Keep the replacement as an editable draft. Media templates imported
        # from Meta may not have reusable approval files locally; the admin can
        # upload the original file and submit without rebuilding the template.
        return JsonResponse(
            {
                "ok": True,
                "status": "draft_needs_review",
                "template_id": str(copied.pk),
                "edit_url": edit_url,
                "message": (
                    "A tracked replacement draft was created. Review it before "
                    f"submission: {exc}"
                ),
            },
            status=202,
        )

    return JsonResponse(
        {
            "ok": True,
            "status": "submitted",
            "template_id": str(copied.pk),
            "edit_url": edit_url,
            "message": (
                "Tracked replacement submitted to Meta. Use it after Meta approves it; "
                "the current approved template remains unchanged."
            ),
        },
        status=201,
    )
