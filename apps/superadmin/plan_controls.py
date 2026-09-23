"""Superadmin-only package grants, global tags and confirmed tenant deletion."""

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError, RestrictedError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST
from apps.organizations.features import module_controls
from apps.organizations.models import Organization, OrganizationTag
from .feature_toggle_views import superuser_required
from .models import AuditLog


@superuser_required
@require_POST
def organization_modules_view(request, organization_id):
    with transaction.atomic():
        organization = get_object_or_404(
            Organization.objects.select_for_update(), pk=organization_id
        )
        available = {entry["key"] for entry in module_controls(organization)}
        selected = set(request.POST.getlist("modules"))
        if not available or not selected <= available:
            return HttpResponseBadRequest("Invalid package module selection.")
        settings = dict(organization.settings) if isinstance(organization.settings, dict) else {}
        grants = settings.get("package_module_grants", {})
        grants = dict(grants) if isinstance(grants, dict) else {}
        grants[organization.package] = sorted(selected)
        settings["package_module_grants"] = grants
        organization.settings = settings
        organization.save(update_fields=["settings", "updated_at"])
        AuditLog.record(
            actor=request.user,
            action=AuditLog.Action.ORGANIZATION_UPDATED,
            target=organization,
            request=request,
            changed_field="module_access",
            package=organization.package,
            modules=sorted(selected),
        )
    messages.success(request, "Module access updated. Changes apply immediately.")
    return redirect("superadmin-organization-detail", organization_id=organization_id)


@superuser_required
@require_http_methods(["GET", "POST"])
def organization_delete_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    if request.method == "GET":
        return render(
            request,
            "superadmin/organization_delete.html",
            {"organization": organization},
        )
    if request.POST.get("confirmation") != str(organization.pk):
        return HttpResponseBadRequest(
            "Explicit organisation deletion confirmation is required."
        )
    if request.user.organization_id == organization.pk:
        return HttpResponseBadRequest(
            "Use a platform superadmin outside this organisation to delete it."
        )
    try:
        with transaction.atomic():
            organization = Organization.objects.select_for_update().get(
                pk=organization_id
            )
            AuditLog.record(
                actor=request.user,
                action=AuditLog.Action.ORGANIZATION_DELETED,
                target=organization,
                request=request,
                organization_id=str(organization.pk),
            )
            from apps.organizations.deletion import delete_organization

            delete_organization(organization)
    except (ProtectedError, RestrictedError):
        messages.error(
            request,
            "Deletion blocked by a record belonging to another organisation. Resolve that reference first.",
        )
        return redirect(
            "superadmin-organization-detail", organization_id=organization_id
        )
    messages.success(
        request, "Organisation and its operational records permanently deleted."
    )
    return redirect("superadmin-org-list")


@superuser_required
@require_http_methods(["GET", "POST"])
def organization_tag_manage_view(request):
    if request.method == "POST":
        action = request.POST.get("action")
        tag = None
        if action in {"edit", "delete"}:
            try:
                tag_id = int(request.POST.get("tag_id", ""))
            except (TypeError, ValueError):
                return HttpResponseBadRequest("Invalid tag identifier.")
            tag = get_object_or_404(OrganizationTag, pk=tag_id)
        if action == "delete":
            if request.POST.get("confirmation") != str(tag.pk):
                return HttpResponseBadRequest("Tag deletion confirmation is required.")
            with transaction.atomic():
                AuditLog.record(
                    actor=request.user,
                    action=AuditLog.Action.ORGANIZATION_TAG_DELETED,
                    target=tag,
                    request=request,
                )
                tag.delete()
            messages.success(request, "Tag deleted from all organisations.")
        elif action in {"create", "edit"}:
            name = " ".join(request.POST.get("name", "").split())
            if not name or len(name) > 50:
                messages.error(request, "Enter a tag name between 1 and 50 characters.")
            elif (
                OrganizationTag.objects.filter(name__iexact=name)
                .exclude(pk=getattr(tag, "pk", None))
                .exists()
            ):
                messages.error(request, "A tag with this name already exists.")
            else:
                try:
                    with transaction.atomic():
                        tag = tag or OrganizationTag()
                        tag.name = name
                        tag.save()
                        AuditLog.record(
                            actor=request.user,
                            action=AuditLog.Action.ORGANIZATION_TAG_UPDATED,
                            target=tag,
                            request=request,
                        )
                    messages.success(request, "Tag saved.")
                except IntegrityError:
                    messages.error(request, "A tag with this name already exists.")
        else:
            return HttpResponseBadRequest("Invalid tag action.")
        return redirect("superadmin-tags")
    return render(
        request, "superadmin/tags.html", {"tags": OrganizationTag.objects.all()}
    )
