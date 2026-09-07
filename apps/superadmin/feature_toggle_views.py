from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.organizations.features import set_hosted_account_enabled
from apps.organizations.models import Organization

from .models import AuditLog


def superuser_required(view_func):
    return user_passes_test(
        lambda user: user.is_authenticated and user.is_superuser,
        login_url="/superadmin/login/",
    )(view_func)


@superuser_required
@require_POST
def organization_hosted_account_toggle_view(request, organization_id):
    """Enable or disable Hosted Account access for one organization."""
    organization = get_object_or_404(Organization, pk=organization_id)
    value = request.POST.get("enabled", "")

    if value not in {"0", "1"}:
        messages.error(request, "Invalid Hosted Account setting.")
        return redirect(
            "superadmin-organization-update",
            organization_id=organization.id,
        )

    enabled = value == "1"
    set_hosted_account_enabled(organization, enabled)

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
    )

    messages.success(
        request,
        f"Hosted Account {'enabled' if enabled else 'disabled'} for {organization.name}.",
    )
    return redirect(
        "superadmin-organization-update",
        organization_id=organization.id,
    )
