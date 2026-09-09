from django.contrib import messages
from django.contrib.auth.forms import SetPasswordForm
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Organization, OrganizationTag

from .models import AuditLog
from .views_flat import superuser_required


def _organization_detail_url(organization, *, query="", anchor=""):
    url = reverse(
        "superadmin-organization-detail",
        kwargs={"organization_id": organization.id},
    )
    if query:
        url = f"{url}?{query}"
    if anchor:
        url = f"{url}#{anchor}"
    return url


@superuser_required
def organization_notes_update_view(request, organization_id):
    """Update internal operational notes from the Superadmin workspace."""
    organization = get_object_or_404(Organization, pk=organization_id)

    if request.method != "POST":
        return redirect(_organization_detail_url(organization, anchor="workspace"))

    notes = request.POST.get("operational_notes", "").strip()
    now = timezone.now()

    settings_payload = dict(organization.settings or {})
    settings_payload["operational_notes_updated_at"] = now.isoformat()

    organization.operational_notes = notes
    organization.settings = settings_payload
    organization.save(
        update_fields=["operational_notes", "settings", "updated_at"],
    )

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
        organization_id=str(organization.id),
        changed_field="operational_notes",
    )
    messages.success(request, "Operational notes saved.")
    return redirect(_organization_detail_url(organization, anchor="workspace"))


@superuser_required
def organization_tags_update_view(request, organization_id):
    """Replace organization tags from the compact Superadmin tag editor."""
    organization = get_object_or_404(Organization, pk=organization_id)

    if request.method != "POST":
        return redirect(_organization_detail_url(organization, anchor="workspace"))

    raw_tags = request.POST.get("tags", "")
    names = []
    seen = set()

    for raw_name in raw_tags.split(","):
        name = " ".join(raw_name.strip().split())
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        if len(name) > 50:
            messages.error(
                request,
                f'Tag "{name[:32]}…" is longer than 50 characters.',
            )
            return redirect(
                _organization_detail_url(
                    organization,
                    query="edit_tags=1",
                    anchor="workspace",
                )
            )
        names.append(name)
        seen.add(key)

    if len(names) > 20:
        messages.error(request, "Use no more than 20 tags on one organization.")
        return redirect(
            _organization_detail_url(
                organization,
                query="edit_tags=1",
                anchor="workspace",
            )
        )

    with transaction.atomic():
        tags = []
        for name in names:
            tag = OrganizationTag.objects.filter(name__iexact=name).first()
            if tag is None:
                tag = OrganizationTag.objects.create(name=name)
            tags.append(tag)
        organization.tags.set(tags)

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
        organization_id=str(organization.id),
        changed_field="tags",
        tags=names,
    )
    messages.success(request, "Organization tags updated.")
    return redirect(_organization_detail_url(organization, anchor="workspace"))


@superuser_required
def organization_user_reset_password_view(request, organization_id):
    """Reset an organization user's password and always surface validation feedback."""
    organization = get_object_or_404(Organization, pk=organization_id)

    if request.method != "POST":
        return redirect(
            _organization_detail_url(
                organization,
                query="reset_password=1",
                anchor="account-controls",
            )
        )

    user_id = request.POST.get("user_id", "").strip()
    if not user_id:
        messages.error(request, "Choose an organization user first.")
        return redirect(
            _organization_detail_url(
                organization,
                query="reset_password=1",
                anchor="account-controls",
            )
        )

    user = get_object_or_404(
        User,
        pk=user_id,
        organization=organization,
        is_superuser=False,
    )
    form = SetPasswordForm(user, request.POST)

    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, str(error))
        return redirect(
            _organization_detail_url(
                organization,
                query=f"reset_password=1&reset_user={user.id}",
                anchor="account-controls",
            )
        )

    form.save()
    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.PASSWORD_RESET,
        target=user,
        request=request,
        organization_id=str(organization.id),
    )
    messages.success(request, f"Password reset successfully for {user.email}.")
    return redirect(_organization_detail_url(organization, anchor="account-controls"))
