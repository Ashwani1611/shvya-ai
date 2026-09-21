from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.integrations.diagnostic_auth import (
    request_fingerprint,
    sanitize_text,
)
from apps.integrations.operations_auth import (
    end_support_context_record,
    revoke_token_record,
)
from apps.integrations.operations_models import (
    OperationsAuditEvent,
    OperationsOAuthAuthorizationCode,
    OperationsOAuthToken,
    OperationsPolicy,
    OperationsSupportSession,
)
from apps.integrations.operations_policy import (
    ALL_CAPABILITIES,
    DEFAULT_APPROVAL_REQUIRED,
    DEFAULT_ORG_CAPABILITIES,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    WRITE_CAPABILITIES,
)
from apps.organizations.models import Organization
from apps.superadmin.models import AuditLog
from apps.superadmin.views_flat import superuser_required


def _record_superadmin_operations_event(
    *,
    actor,
    organization,
    tool_name,
    target_type,
    target_id,
    reason,
    change_summary,
    support_session=None,
):
    return OperationsAuditEvent.objects.create(
        actor=actor,
        role=ROLE_SUPERADMIN,
        organization=organization,
        support_session=support_session,
        tool_name=tool_name[:100],
        capability="",
        target_type=target_type[:80],
        target_id=str(target_id)[:100],
        reason=sanitize_text(reason, limit=500),
        outcome=OperationsAuditEvent.Outcome.SUCCESS,
        request_fingerprint=request_fingerprint(
            {
                "event": tool_name,
                "actor_id": str(actor.id),
                "organization_id": str(organization.id),
                "target_type": target_type,
                "target_id": str(target_id),
            }
        ),
        change_summary=change_summary,
        duration_ms=0,
        error_code="",
    )


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
        if item in allowed and item in WRITE_CAPABILITIES
    ]

    revoked_token_count = 0
    expired_code_count = 0
    with transaction.atomic():
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

        if not enabled:
            now = timezone.now()
            revoked_token_count = OperationsOAuthToken.objects.filter(
                organization=organization,
                role=ROLE_ORGANIZATION_ADMIN,
                revoked_at__isnull=True,
            ).update(
                revoked_at=now,
                updated_at=now,
            )
            expired_code_count = (
                OperationsOAuthAuthorizationCode.objects.filter(
                    organization=organization,
                    role=ROLE_ORGANIZATION_ADMIN,
                    used_at__isnull=True,
                    expires_at__gt=now,
                ).update(
                    expires_at=now,
                )
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
        operations_mcp_revoked_token_count=revoked_token_count,
        operations_mcp_expired_authorization_code_count=expired_code_count,
    )
    _record_superadmin_operations_event(
        actor=request.user,
        organization=organization,
        tool_name="operations_policy_update",
        target_type="organization",
        target_id=organization.id,
        reason="SHVYA Superadmin updated External AI Operations policy.",
        change_summary={
            "organization_admin_enabled": enabled,
            "allowed_capabilities": sorted(allowed),
            "approval_required_capabilities": sorted(approval),
            "revoked_token_count": revoked_token_count,
            "expired_authorization_code_count": expired_code_count,
        },
    )

    message = "External AI Operations policy updated for this organization."
    if revoked_token_count or expired_code_count:
        message += (
            f" Revoked {revoked_token_count} active Organization Admin "
            f"Operations session(s) and expired {expired_code_count} pending "
            "authorization code(s); fresh authorization is required to reconnect."
        )
    messages.success(request, message)
    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )



@superuser_required
@require_POST
def organization_operations_session_revoke_view(
    request,
    organization_id,
    token_id,
):
    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )
    token = get_object_or_404(
        OperationsOAuthToken.objects.select_related(
            "actor",
            "client",
        ),
        pk=token_id,
        organization=organization,
        role=ROLE_ORGANIZATION_ADMIN,
        revoked_at__isnull=True,
    )
    revoked = revoke_token_record(token=token)
    if revoked is not None:
        _record_superadmin_operations_event(
            actor=request.user,
            organization=organization,
            tool_name="oauth_revoke_superadmin_dashboard",
            target_type="oauth_grant",
            target_id=token.id,
            reason=(
                "SHVYA Superadmin revoked an Organization Admin "
                "External AI Operations session."
            ),
            change_summary={
                "client_name": sanitize_text(
                    token.client.client_name or "External AI",
                    limit=120,
                ),
                "session_owner": sanitize_text(
                    token.actor.name or "Organization Admin",
                    limit=120,
                ),
                "scopes": sorted(set(str(token.scope or "").split())),
                "access": "revoked",
            },
        )
        messages.success(
            request,
            "External AI Operations session revoked for this organization.",
        )
    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


@superuser_required
@require_POST
def organization_operations_support_end_view(
    request,
    organization_id,
    session_id,
):
    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )
    support_session = get_object_or_404(
        OperationsSupportSession.objects.select_related(
            "actor",
            "token__client",
        ),
        pk=session_id,
        organization=organization,
        ended_at__isnull=True,
        token__role=ROLE_SUPERADMIN,
    )
    ended_count = end_support_context_record(
        token=support_session.token,
        organization=organization,
    )
    if ended_count:
        support_session.refresh_from_db()
        _record_superadmin_operations_event(
            actor=request.user,
            organization=organization,
            tool_name="support_context_force_end",
            target_type="support_session",
            target_id=support_session.id,
            reason=(
                "SHVYA Superadmin force-ended an active External AI "
                "support context from the Superadmin dashboard."
            ),
            change_summary={
                "support_actor": sanitize_text(
                    support_session.actor.name or "SHVYA Support",
                    limit=120,
                ),
                "client_name": sanitize_text(
                    support_session.token.client.client_name or "External AI",
                    limit=120,
                ),
                "support_context": "ended",
            },
            support_session=support_session,
        )
        messages.success(
            request,
            "SHVYA Support Operations context ended for this organization.",
        )
    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )
