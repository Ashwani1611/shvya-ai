"""Tenant-scoped Touchpoint management and encrypted attachment downloads."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_http_methods

from apps.crm.decorators import crm_login_required
from services.touchpoint_service import (
    apply_attachment_changes, validate_attachment_changes,
    validate_reply_placeholders,
)
from .models import TouchpointAttachment, TouchpointCategory, TouchpointReply


@crm_login_required
@require_http_methods(["GET", "POST"])
def touchpoints(request):
    org = request.crm_user.organization
    if not org:
        return JsonResponse({"error": "An organization is required."}, status=403)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            with transaction.atomic():
                if action in {"save_category", "delete_category"}:
                    category_id = request.POST.get("category_id")
                    category = (
                        get_object_or_404(
                            TouchpointCategory, pk=category_id, organization=org
                        )
                        if category_id
                        else TouchpointCategory(organization=org)
                    )
                    if action == "delete_category":
                        if not category_id:
                            raise ValidationError("Choose a category.")
                        category.delete()
                    else:
                        category.name = request.POST.get("name", "").strip()
                        category.full_clean()
                        category.save()
                elif action in {"save_reply", "delete_reply"}:
                    reply_id = request.POST.get("reply_id")
                    reply = (
                        get_object_or_404(
                            TouchpointReply, pk=reply_id, category__organization=org
                        )
                        if reply_id
                        else TouchpointReply()
                    )
                    if action == "delete_reply":
                        if not reply_id:
                            raise ValidationError("Choose a reply.")
                        reply.delete()
                    else:
                        reply.category = get_object_or_404(
                            TouchpointCategory,
                            pk=request.POST.get("category_id"),
                            organization=org,
                        )
                        reply.title = request.POST.get("title", "").strip()
                        reply.body = request.POST.get("body", "").strip()
                        validate_reply_placeholders(organization=org, body=reply.body)
                        reply.full_clean()
                        uploads, remove_ids = validate_attachment_changes(
                            reply=reply,
                            uploads=request.FILES.getlist("attachments"),
                            remove_ids=request.POST.getlist("remove_attachments"),
                        )
                        reply.save()
                        apply_attachment_changes(
                            reply=reply, uploads=uploads, remove_ids=remove_ids,
                        )
                else:
                    raise ValidationError("Unknown action.")
        except (ValidationError, IntegrityError, ValueError) as exc:
            error = (
                " ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "Unable to save. Check the values and use a unique category name."
            )
            return JsonResponse({"error": error}, status=400)
        return JsonResponse({"ok": True})
    categories = TouchpointCategory.objects.filter(organization=org).prefetch_related(
        Prefetch(
            "replies",
            queryset=TouchpointReply.objects.filter(is_active=True).prefetch_related(
                "attachments"
            ),
        )
    )
    return render(request, "followups/touchpoints.html", {"categories": categories})


@crm_login_required
@require_GET
def touchpoint_attachment_download(request, attachment_id):
    """The real file storage has no public URL. Every read checks the tenant."""
    attachment = get_object_or_404(
        TouchpointAttachment.objects.select_related("reply__category"),
        pk=attachment_id, reply__category__organization=request.crm_user.organization,
        reply__is_active=True,
    )
    if not attachment.file:
        raise Http404
    try:
        response = FileResponse(
            attachment.file.open("rb"),
            as_attachment=True,
            filename=attachment.original_name,
            content_type="application/octet-stream",
        )
    except (FileNotFoundError, OSError, ValueError):
        raise Http404("Attachment unavailable.") from None
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
