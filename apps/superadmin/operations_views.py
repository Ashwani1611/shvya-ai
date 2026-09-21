from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.integrations.operations_models import OperationsPolicy
from apps.integrations.operations_policy import (
    ALL_CAPABILITIES,
    DEFAULT_APPROVAL_REQUIRED,
    DEFAULT_ORG_CAPABILITIES,
)
from apps.organizations.models import Organization
from apps.superadmin.models import AuditLog
from apps.superadmin.views_flat import superuser_required


@superuser_required
@require_POST
def organization_operations_policy_update_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    policy, _ = OperationsPolicy.objects.get_or_create(
        organization=organization,
        defaults={
            "allowed_capabilities": DEFAULT_ORG_CAPABILITIES,
            "approval_required_capabilities": DEFAULT_APPROVAL_REQUIRED,
        },
    )

    enabled = request.POST.get("organization_admin_enabled") == "on"
    allowed = [
        item
        for item in request.POST.getlist("allowed_capabilities")
        if item in ALL_CAPABILITIES
    ]
    approval = [
        item
        for item in request.POST.getlist("approval_required_capabilities")
        if item in allowed and item in ALL_CAPABILITIES
    ]

    policy.organization_admin_enabled = enabled
    policy.allowed_capabilities = allowed
    policy.approval_required_capabilities = approval
    policy.updated_by = request.user
    policy.save(
        update_fields=[
            "organization_admin_enabled",
            "allowed_capabilities",
            "approval_required_capabilities",
            "updated_by",
            "updated_at",
        ]
    )

    AuditLog.record(
        actor=request.user,
        action=AuditLog.Action.ORGANIZATION_UPDATED,
        target=organization,
        request=request,
        changed_fields=[
            "operations_mcp.organization_admin_enabled",
            "operations_mcp.allowed_capabilities",
            "operations_mcp.approval_required_capabilities",
        ],
        operations_mcp_enabled=enabled,
        operations_mcp_capability_count=len(allowed),
    )
    messages.success(
        request,
        "External AI Operations policy updated for this organization.",
    )
    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )
