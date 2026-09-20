"""Tenant-scoped category and quick reply management."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from apps.crm.decorators import crm_login_required
from .models import TouchpointCategory, TouchpointReply


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
                        reply.full_clean()
                        reply.save()
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
        "replies"
    )
    return render(request, "followups/touchpoints.html", {"categories": categories})
